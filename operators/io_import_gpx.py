# -*- coding:utf-8 -*-
import os
import xml.etree.ElementTree as ET

import bmesh
import bpy
from bpy.types import Operator
from bpy.props import (
        StringProperty,
        BoolProperty,
        EnumProperty,
        FloatProperty,
)
from bpy_extras.io_utils import ImportHelper

import logging
log = logging.getLogger(__name__)

from ..geoscene import GeoScene
from ..core import BBOX
from ..core.proj import Reproj
from ..core.utils import perf_clock
from .utils import adjust3Dview, DropToGround

PKG, SUBPKG = __package__.split('.', maxsplit=1)


def _get_namespace(tag):
        if tag.startswith('{'):
                return tag.split('}')[0].strip('{')
        return ''


class IMPORTGIS_OT_gpx(Operator, ImportHelper):
        """Import GPX tracks"""

        bl_idname = "importgis.gpx_file"
        bl_description = 'Import GPX track file (.gpx)'
        bl_label = "Import GPX"
        bl_options = {"UNDO"}

        filename_ext = ".gpx"

        filter_glob: StringProperty(
                default="*.gpx",
                options={'HIDDEN'}
        )

        elevation_mode: EnumProperty(
                name="Elevation",
                description="Choose how to build z coordinates",
                items=[
                        ('FILE', 'GPX / Default', "Use GPX elevation when available, fallback to default value"),
                        ('CONSTANT', 'Constant', "Apply the default elevation to every point"),
                        ('OBJECT', 'Ground object', "Sample elevation from an existing ground mesh"),
                ],
                default='FILE'
        )

        default_elevation: FloatProperty(
                name="Default elevation",
                description="Elevation to use when GPX point has no z component",
                default=0.0
        )

        elevation_offset: FloatProperty(
                name="Ground offset",
                description="Add vertical offset to sampled ground elevation",
                default=0.0
        )

        separate_objects: BoolProperty(
                name="Separate tracks",
                description="Create a separate object for each GPX track",
                default=True
        )

        def listObjects(self, context):
                objs = []
                for obj in bpy.context.scene.objects:
                        if obj.type == 'MESH':
                                objs.append((obj.name, obj.name, "Object named " + obj.name))
                return objs

        ground_object: EnumProperty(
                name="Ground object",
                description="Choose the mesh from which to extract elevation",
                items=listObjects
        )

        @classmethod
        def poll(cls, context):
                return context.mode == 'OBJECT'

        def draw(self, context):
                layout = self.layout
                layout.prop(self, 'elevation_mode')
                if self.elevation_mode == 'OBJECT':
                        layout.prop(self, 'ground_object')
                        layout.prop(self, 'elevation_offset')
                layout.prop(self, 'default_elevation')
                layout.prop(self, 'separate_objects')

        def _parse_tracks(self):
                try:
                        tree = ET.parse(self.filepath)
                        root = tree.getroot()
                except Exception:
                        log.error('Unable to parse GPX file', exc_info=True)
                        self.report({'ERROR'}, "Unable to parse GPX file, check logs")
                        return []

                ns_uri = _get_namespace(root.tag)
                ns_prefix = f"{{{ns_uri}}}" if ns_uri else ''

                tracks = []
                for idx, trk in enumerate(root.findall(f'.//{ns_prefix}trk')):
                        name = trk.findtext(f'{ns_prefix}name') or f"Track {idx+1}"
                        segments = []
                        for seg in trk.findall(f'{ns_prefix}trkseg'):
                                pts = []
                                for pt in seg.findall(f'{ns_prefix}trkpt'):
                                        try:
                                                lat = float(pt.attrib.get('lat'))
                                                lon = float(pt.attrib.get('lon'))
                                        except (TypeError, ValueError):
                                                continue
                                        ele_tag = pt.find(f'{ns_prefix}ele')
                                        try:
                                                ele = float(ele_tag.text) if ele_tag is not None else None
                                        except (TypeError, ValueError):
                                                ele = None
                                        pts.append((lon, lat, ele))
                                if len(pts) >= 2:
                                        segments.append(pts)
                        if segments:
                                tracks.append({'name': name, 'segments': segments})

                return tracks

        def _project_tracks(self, tracks, rprj):
                projected_tracks = []
                all_coords = []

                for track in tracks:
                        proj_segments = []
                        for seg in track['segments']:
                                coords = [(pt[0], pt[1]) for pt in seg]
                                if rprj is not None:
                                        coords = rprj.pts(coords)
                                proj_pts = []
                                for (x, y), (_, _, ele) in zip(coords, seg):
                                        proj_pts.append((x, y, ele))
                                        all_coords.append((x, y))
                                proj_segments.append(proj_pts)
                        projected_tracks.append({'name': track['name'], 'segments': proj_segments})

                if not all_coords:
                        return projected_tracks, None

                xs, ys = zip(*all_coords)
                bbox = BBOX(min(xs), min(ys), max(xs), max(ys))
                return projected_tracks, bbox

        def _get_elevation(self, rayCaster, x, y, ele):
                if self.elevation_mode == 'OBJECT':
                        rc_hit = rayCaster.rayCast(x=x, y=y)
                        return rc_hit.loc.z + self.elevation_offset
                if self.elevation_mode == 'CONSTANT':
                        return self.default_elevation
                return ele if ele is not None else self.default_elevation

        def execute(self, context):
                prefs = bpy.context.preferences.addons[PKG].preferences

                w = context.window
                w.cursor_set('WAIT')
                t0 = perf_clock()

                try:
                        tracks = self._parse_tracks()
                        if not tracks:
                                return {'CANCELLED'}

                        geoscn = GeoScene()
                        if geoscn.isBroken:
                                self.report({'ERROR'}, "Scene georef is broken, please fix it beforehand")
                                return {'CANCELLED'}

                        gpxCRS = 4326
                        if not geoscn.hasCRS:
                                geoscn.crs = gpxCRS

                        try:
                                rprj = Reproj(gpxCRS, geoscn.crs)
                        except Exception:
                                log.error('Unable to init reprojection', exc_info=True)
                                self.report({'ERROR'}, "Unable to init reprojection, check logs")
                                return {'CANCELLED'}

                        projected_tracks, bbox = self._project_tracks(tracks, rprj)
                        if bbox is None:
                                self.report({'ERROR'}, "No coordinates found in GPX file")
                                return {'CANCELLED'}

                        if not geoscn.isGeoref:
                                dx, dy = bbox.center
                                geoscn.setOriginPrj(dx, dy)
                        else:
                                dx, dy = geoscn.getOriginPrj()

                        rayCaster = None
                        if self.elevation_mode == 'OBJECT':
                                try:
                                        elev_obj = bpy.context.scene.objects[self.ground_object]
                                except Exception:
                                        self.report({'ERROR'}, "Cannot find selected ground object")
                                        return {'CANCELLED'}
                                rayCaster = DropToGround(bpy.context.scene, elev_obj)

                        created_objs = []
                        target_collection = None
                        if self.separate_objects:
                                collection_name = os.path.splitext(os.path.basename(self.filepath))[0]
                                target_collection = bpy.data.collections.new(collection_name)
                                context.scene.collection.children.link(target_collection)

                        if not self.separate_objects:
                                bm = bmesh.new()

                        for track_idx, track in enumerate(projected_tracks):
                                if self.separate_objects:
                                        bm = bmesh.new()

                                for seg in track['segments']:
                                        verts = []
                                        for x, y, ele in seg:
                                                vx = x - dx
                                                vy = y - dy
                                                vz = self._get_elevation(rayCaster, vx, vy, ele)
                                                verts.append(bm.verts.new((vx, vy, vz)))
                                        for i in range(len(verts) - 1):
                                                try:
                                                        bm.edges.new((verts[i], verts[i+1]))
                                                except ValueError:
                                                        # Skip duplicate edges
                                                        continue

                                if self.separate_objects:
                                        name = track['name'] or f"Track {track_idx+1}"
                                        mesh = bpy.data.meshes.new(name)
                                        bm.to_mesh(mesh)
                                        mesh.validate(verbose=False)
                                        obj = bpy.data.objects.new(name, mesh)
                                        (target_collection or context.scene.collection).objects.link(obj)
                                        created_objs.append(obj)
                                        bm.free()

                        if not self.separate_objects:
                                name = os.path.splitext(os.path.basename(self.filepath))[0]
                                mesh = bpy.data.meshes.new(name)
                                bm.to_mesh(mesh)
                                mesh.validate(verbose=False)
                                obj = bpy.data.objects.new(name, mesh)
                                context.scene.collection.objects.link(obj)
                                created_objs.append(obj)
                                bm.free()

                        if created_objs:
                                bpy.ops.object.select_all(action='DESELECT')
                                for obj in created_objs:
                                        obj.select_set(True)
                                context.view_layer.objects.active = created_objs[0]
                                bpy.ops.object.origin_set(type='ORIGIN_GEOMETRY')

                        if prefs.adjust3Dview:
                                bbox.shift(-dx, -dy)
                                adjust3Dview(context, bbox)

                        t = perf_clock() - t0
                        log.info('GPX build in %f seconds' % t)

                        return {'FINISHED'}
                finally:
                        w.cursor_set('DEFAULT')


classes = [
        IMPORTGIS_OT_gpx,
]


def register():
        for cls in classes:
                try:
                        bpy.utils.register_class(cls)
                except ValueError:
                        log.warning('{} is already registered, now unregister and retry... '.format(cls))
                        bpy.utils.unregister_class(cls)
                        bpy.utils.register_class(cls)


def unregister():
        for cls in classes:
                bpy.utils.unregister_class(cls)
