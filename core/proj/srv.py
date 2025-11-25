# -*- coding:utf-8 -*-

#  ***** GPL LICENSE BLOCK *****
#
#  This program is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program.  If not, see <http://www.gnu.org/licenses/>.
#  All rights reserved.
#  ***** GPL LICENSE BLOCK *****
import logging
log = logging.getLogger(__name__)


import os
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from urllib.parse import quote_plus
import json

from .. import settings

USER_AGENT = settings.user_agent

DEFAULT_TIMEOUT = 2
REPROJ_TIMEOUT = 60

######################################
# EPSG.io
# https://github.com/klokantech/epsg.io


class EPSGIO():

    @staticmethod
    def ping(api_key=None):
        api_key = api_key or os.environ.get("MAPTILER_API_KEY")

        if api_key:
            url = f"https://api.maptiler.com/coordinates/search/4326.json?key={api_key}&limit=1"
        else:
            url = "https://epsg.io"

        try:
            rq = Request(url, headers={'User-Agent': USER_AGENT})
            urlopen(rq, timeout=DEFAULT_TIMEOUT)
            return True
        except URLError as e:
            log.error('Cannot ping %s web service, %s', url, e.reason)
            return False
        except HTTPError as e:
            log.error('Cannot ping %s web service, http error %s', url, e.code)
            return False
        except Exception:
            raise


    @staticmethod
    def reprojPt(epsg1, epsg2, x1, y1):

        url = "http://epsg.io/trans?x={X}&y={Y}&z={Z}&s_srs={CRS1}&t_srs={CRS2}"

        url = url.replace("{X}", str(x1))
        url = url.replace("{Y}", str(y1))
        url = url.replace("{Z}", '0')
        url = url.replace("{CRS1}", str(epsg1))
        url = url.replace("{CRS2}", str(epsg2))

        log.debug(url)

        try:
            rq = Request(url, headers={'User-Agent': USER_AGENT})
            response = urlopen(rq, timeout=REPROJ_TIMEOUT).read().decode('utf8')
        except (URLError, HTTPError) as err:
            log.error('Http request fails url:{}, code:{}, error:{}'.format(url, err.code, err.reason))
            raise

        obj = json.loads(response)

        return (float(obj['x']), float(obj['y']))

    @staticmethod
    def reprojPts(epsg1, epsg2, points):

        if len(points) == 1:
            x, y = points[0]
            return [EPSGIO.reprojPt(epsg1, epsg2, x, y)]

        urlTemplate = "http://epsg.io/trans?data={POINTS}&s_srs={CRS1}&t_srs={CRS2}"

        urlTemplate = urlTemplate.replace("{CRS1}", str(epsg1))
        urlTemplate = urlTemplate.replace("{CRS2}", str(epsg2))

        #data = ';'.join([','.join(map(str, p)) for p in points])

        precision = 4
        data = [','.join( [str(round(v, precision)) for v in p] ) for p in points ]
        part, parts = [], []
        for i,p in enumerate(data):
            l = sum([len(p) for p in part]) + len(';'*len(part))
            if l + len(p) < 4000: #limit is 4094
                part.append(p)
            else:
                parts.append(part)
                part = [p]
            if i == len(data)-1:
                parts.append(part)
        parts = [';'.join(part) for part in parts]

        result = []
        for part in parts:
            url = urlTemplate.replace("{POINTS}", part)
            log.debug(url)

            try:
                rq = Request(url, headers={'User-Agent': USER_AGENT})
                response = urlopen(rq, timeout=REPROJ_TIMEOUT).read().decode('utf8')
            except (URLError, HTTPError) as err:
                log.error('Http request fails url:{}, code:{}, error:{}'.format(url, err.code, err.reason))
                raise

            obj = json.loads(response)
            result.extend( [(float(p['x']), float(p['y'])) for p in obj] )

        return result

    @staticmethod
    def _build_maptiler_url(query, api_key):
        encoded_query = quote_plus(str(query))
        return f"https://api.maptiler.com/coordinates/search/{encoded_query}.json?key={api_key}"

    @staticmethod
    def _normalize_results(obj):
        results = obj.get('results') or obj.get('crs') or obj.get('coordinateSystems') or []

        normalized = []
        for item in results:
            code = str(item.get('code') or item.get('epsg') or item.get('identifier') or '').strip()
            name = item.get('name') or item.get('title') or ''
            if not code or not name:
                continue
            normalized.append({'code': code, 'name': name})

        return normalized

    @staticmethod
    def search(query, api_key=None):
        api_key = api_key or os.environ.get("MAPTILER_API_KEY")

        if api_key:
            url = EPSGIO._build_maptiler_url(query, api_key)
        else:
            query = quote_plus(str(query))
            url = f"https://epsg.io/?q={query}&format=json"

        log.debug('Search crs : %s', url)
        rq = Request(url, headers={'User-Agent': USER_AGENT})

        try:
            response = urlopen(rq, timeout=DEFAULT_TIMEOUT).read().decode('utf8')
        except HTTPError as err:
            log.error('Http request fails url:%s, code:%s, error:%s', url, err.code, err.reason)
            return []
        except URLError as err:
            log.error('Http request fails url:%s, error:%s', url, err.reason)
            return []

        if not response:
            log.error('Http request to %s returned an empty response', url)
            return []

        if response.lstrip().startswith('<'):
            if api_key:
                log.error('Unexpected HTML response from %s, please verify your MapTiler API key and connectivity.', url)
            else:
                log.error('Got an HTML response from %s. EPSG search endpoints now redirect to the MapTiler Coordinates API; please configure access to that service or provide a MapTiler API key.', url)
            return []

        try:
            obj = json.loads(response)
        except json.JSONDecodeError:
            snippet = response[:500] + ('…' if len(response) > 500 else '')
            log.error('Unable to decode response from %s : %s', url, snippet)
            return []

        results = EPSGIO._normalize_results(obj)

        log.debug('Search results : %s', [(r['code'], r['name']) for r in results])
        return results

    @staticmethod
    def getEsriWkt(epsg):
        url = "http://epsg.io/{CODE}.esriwkt"
        url = url.replace("{CODE}", str(epsg))
        log.debug(url)
        rq = Request(url, headers={'User-Agent': USER_AGENT})
        wkt = urlopen(rq, timeout=DEFAULT_TIMEOUT).read().decode('utf8')
        return wkt




######################################
# World Coordinate Converter
# https://github.com/ClemRz/TWCC

class TWCC():

    @staticmethod
    def reprojPt(epsg1, epsg2, x1, y1):

        url = "http://twcc.fr/en/ws/?fmt=json&x={X}&y={Y}&in=EPSG:{CRS1}&out=EPSG:{CRS2}"

        url = url.replace("{X}", str(x1))
        url = url.replace("{Y}", str(y1))
        url = url.replace("{Z}", '0')
        url = url.replace("{CRS1}", str(epsg1))
        url = url.replace("{CRS2}", str(epsg2))

        rq = Request(url, headers={'User-Agent': USER_AGENT})
        response = urlopen(rq, timeout=REPROJ_TIMEOUT).read().decode('utf8')
        obj = json.loads(response)

        return (float(obj['point']['x']), float(obj['point']['y']))


######################################
#http://spatialreference.org/ref/epsg/2154/esriwkt/

#class SpatialRefOrg():



######################################
#http://prj2epsg.org/search
