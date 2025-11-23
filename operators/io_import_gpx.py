# -*- coding:utf-8 -*-
import os
import xml.etree.ElementTree as ET

import bpy
from bpy.types import Operator
from bpy.props import StringProperty, BoolProperty, EnumProperty

from ..geoscene import GeoScene
from ..core.proj import Reproj
from .utils import DropToGround, adjust3Dview, getBBOX

import logging
log = logging.getLogger(__name__)

PKG, SUBPKG = __package__.split('.', maxsplit=1)


def _iter_points(parent, tag):
    for elem in parent.findall(f".//{{*}}{tag}"):
        lat = elem.get("lat")
        lon = elem.get("lon")
        if lat is None or lon is None:
            continue
        ele_elem = elem.find("{*}ele")
        try:
            z = float(ele_elem.text) if ele_elem is not None else None
        except (TypeError, ValueError):
            z = None
        try:
            yield float(lon), float(lat), z
        except (TypeError, ValueError):
            continue


def _parse_gpx(filepath):
    tree = ET.parse(filepath)
    root = tree.getroot()

    tracks = []
    for trk in root.findall(".//{*}trk"):
        name_elem = trk.find("{*}name")
        trk_name = name_elem.text if name_elem is not None else "Track"
        for i, seg in enumerate(trk.findall("{*}trkseg")):
            pts = list(_iter_points(seg, "trkpt"))
            if pts:
                label = f"{trk_name} {i+1}" if i else trk_name
                tracks.append((label, pts))

    routes = []
    for i, rte in enumerate(root.findall(".//{*}rte")):
        name_elem = rte.find("{*}name")
        rte_name = name_elem.text if name_elem is not None else f"Route {i+1}"
        pts = list(_iter_points(rte, "rtept"))
        if pts:
            routes.append((rte_name, pts))

    waypoints = list(_iter_points(root, "wpt"))
    return tracks, routes, waypoints


class IMPORTGIS_OT_gpx_file_dialog(Operator):
    """Select GPX file and open options dialog"""

    bl_idname = "importgis.gpx_file_dialog"
    bl_description = "Import GPS exchange file (.gpx)"
    bl_label = "Import GPX"
    bl_options = {'INTERNAL'}

    filepath: StringProperty(
        name="File Path",
        description="File path used for importing the file",
        maxlen=1024,
        subtype='FILE_PATH',
    )

    filter_glob: StringProperty(default="*.gpx", options={'HIDDEN'})

    filename_ext = ".gpx"

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        if os.path.exists(self.filepath):
            bpy.ops.importgis.gpx_props_dialog('INVOKE_DEFAULT', filepath=self.filepath)
        else:
            self.report({'ERROR'}, "Invalid filepath")
        return {'FINISHED'}


class IMPORTGIS_OT_gpx_props_dialog(Operator):
    """GPX importer properties dialog"""

    bl_idname = "importgis.gpx_props_dialog"
    bl_description = "Import GPS exchange file (.gpx)"
    bl_label = "Import GPX"
    bl_options = {"INTERNAL"}

    filepath: StringProperty()

    def listObjects(self, context):
        objs = []
        for index, obj in enumerate(context.scene.objects):
            if obj.type == 'MESH':
                objs.append((str(index), obj.name, f"Object named {obj.name}"))
        return objs

    useElevObj: BoolProperty(
        name="Drape on terrain",
        description="Use a mesh object to derive elevation for points without Z",
        default=False,
    )

    objElevLst: EnumProperty(
        name="Elevation object",
        description="Choose the mesh from which extract z elevation",
        items=listObjects,
    )

    drapeMissingOnly: BoolProperty(
        name="Only when Z missing",
        description="Only sample terrain for points without elevation values",
        default=True,
    )

    includeWaypoints: BoolProperty(
        name="Import waypoints",
        description="Create point objects for waypoints",
        default=True,
    )

    def _prefill_elevation_choice(self, context):
        meshes = self.listObjects(context)
        if meshes and not self.objElevLst:
            # Preselect the first mesh so draping works without extra clicks
            self.objElevLst = meshes[0][0]
            self.useElevObj = True

    def invoke(self, context, event):
        self._prefill_elevation_choice(context)
        return context.window_manager.invoke_props_dialog(self)

    def check(self, context):
        return True

    def draw(self, context):
        layout = self.layout
        self._prefill_elevation_choice(context)
        layout.prop(self, "useElevObj")
        if self.useElevObj:
            layout.prop(self, "objElevLst")
            layout.prop(self, "drapeMissingOnly")
        layout.prop(self, "includeWaypoints")

    def execute(self, context):
        if not os.path.exists(self.filepath):
            self.report({'ERROR'}, "Invalid filepath")
            return {'CANCELLED'}

        try:
            tracks, routes, waypoints = _parse_gpx(self.filepath)
        except Exception:
            log.error("Unable to read GPX file", exc_info=True)
            self.report({'ERROR'}, "Unable to read GPX file, see logs for details")
            return {'CANCELLED'}

        if not tracks and not routes and not waypoints:
            self.report({'ERROR'}, "GPX file contains no geometry")
            return {'CANCELLED'}

        geoscn = GeoScene(context.scene)
        if geoscn.isBroken:
            self.report({'ERROR'}, "Scene georef is broken, please fix it beforehand")
            return {'CANCELLED'}

        if not geoscn.hasCRS:
            geoscn.crs = 4326

        rprj = None
        if geoscn.crs != 4326:
            try:
                rprj = Reproj(4326, geoscn.crs)
            except Exception:
                log.error('Reprojection fails', exc_info=True)
                self.report({'ERROR'}, "Unable to reproject GPX data")
                return {'CANCELLED'}

        all_pts = []
        for _, seq in tracks + routes:
            all_pts.extend(seq)
        all_pts.extend(waypoints)
        if not all_pts:
            self.report({'ERROR'}, "GPX file contains no geometry")
            return {'CANCELLED'}

        def _transform_points(seq):
            pts_xy = [(pt[0], pt[1]) for pt in seq]
            if rprj:
                pts_xy = rprj.pts(pts_xy)
            result = []
            for (x, y), (_, _, z) in zip(pts_xy, seq):
                result.append((x, y, z))
            return result

        projected = _transform_points(all_pts)
        if not geoscn.hasOriginPrj:
            first_x, first_y, _ = projected[0]
            geoscn.setOriginPrj(first_x, first_y)

        dx, dy = geoscn.crsx, geoscn.crsy

        elev_obj = None
        rayCaster = None
        if self.useElevObj:
            if not self.objElevLst:
                meshes = self.listObjects(context)
                if meshes:
                    self.objElevLst = meshes[0][0]
            if not self.objElevLst:
                self.report({'ERROR'}, "No mesh selected to sample elevation from")
                return {'CANCELLED'}
            try:
                elev_obj = context.scene.objects[int(self.objElevLst)]
            except (ValueError, IndexError, KeyError):
                elev_obj = None
            if elev_obj:
                rayCaster = DropToGround(context.scene, elev_obj)

        created = []

        def _drape_point(x, y, z):
            if rayCaster is None:
                return (x - dx, y - dy, z if z is not None else 0)
            needs_drape = z is None or not self.drapeMissingOnly
            if needs_drape:
                hit = rayCaster.rayCast(x - dx, y - dy)
                if hit.hit:
                    return hit.loc
            return (x - dx, y - dy, z if z is not None else 0)

        def _add_curve(name, pts):
            if len(pts) < 2:
                return None
            curve = bpy.data.curves.new(name=name, type='CURVE')
            curve.dimensions = '3D'
            spline = curve.splines.new('POLY')
            spline.points.add(len(pts) - 1)
            for i, (x, y, z) in enumerate(pts):
                spline.points[i].co = (x, y, z, 1)
            obj = bpy.data.objects.new(name, curve)
            context.scene.collection.objects.link(obj)
            return obj

        for name, seq in tracks:
            coords = _transform_points(seq)
            coords = [_drape_point(x, y, z) for x, y, z in coords]
            obj = _add_curve(name, coords)
            if obj:
                created.append(obj)

        for name, seq in routes:
            coords = _transform_points(seq)
            coords = [_drape_point(x, y, z) for x, y, z in coords]
            obj = _add_curve(name, coords)
            if obj:
                created.append(obj)

        if self.includeWaypoints:
            for i, (x, y, z) in enumerate(_transform_points(waypoints)):
                wx, wy, wz = _drape_point(x, y, z)
                mesh = bpy.data.meshes.new(f"Waypoint {i+1}")
                mesh.from_pydata([(wx, wy, wz)], [], [])
                mesh.update()
                obj = bpy.data.objects.new(f"Waypoint {i+1}", mesh)
                context.scene.collection.objects.link(obj)
                created.append(obj)

        if created:
            bbox = getBBOX.fromObj(created[0])
            for obj in created[1:]:
                bbox += getBBOX.fromObj(obj)
            adjust3Dview(context, bbox)

        return {'FINISHED'}


def register():
    bpy.utils.register_class(IMPORTGIS_OT_gpx_file_dialog)
    bpy.utils.register_class(IMPORTGIS_OT_gpx_props_dialog)


def unregister():
    bpy.utils.unregister_class(IMPORTGIS_OT_gpx_props_dialog)
    bpy.utils.unregister_class(IMPORTGIS_OT_gpx_file_dialog)