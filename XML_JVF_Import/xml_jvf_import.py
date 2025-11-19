# -*- coding: utf-8 -*-
from qgis.PyQt.QtCore import QVariant
from qgis.PyQt.QtGui import QIcon, QColor
from qgis.PyQt.QtWidgets import QAction, QFileDialog
from qgis.core import (
    QgsVectorLayer, QgsFeature, QgsGeometry, QgsPointXY, QgsField,
    QgsProject, QgsSymbol, QgsSimpleMarkerSymbolLayer, QgsSimpleLineSymbolLayer,
    QgsSimpleFillSymbolLayer, QgsRuleBasedRenderer, QgsWkbTypes, QgsSimpleMarkerSymbolLayerBase
)

import xml.etree.ElementTree as ET
import os
import re
import importlib.util

# cesta k plugin složce
_PLUGIN_DIR = os.path.dirname(__file__)

# Robustní načtení style_map.py (podporuje různé názvy proměnných)
try:
    # nejprve relativní import (normální případ, když je plugin načten jako balíček)
    from .style_map import STYLE_MAP, POVRCHOVE_ZNAKY_RULES
except Exception:
    # fallback: načíst style_map.py přímo z plugin složky pomocí importlib
    STYLE_MAP = {"default": {"color": "#808080"}}
    POVRCHOVE_ZNAKY_RULES = {}
    try:
        _sm_path = os.path.join(_PLUGIN_DIR, "style_map.py")
        if os.path.exists(_sm_path):
            spec = importlib.util.spec_from_file_location("style_map_plugin", _sm_path)
            sm_mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(sm_mod)
            STYLE_MAP = getattr(sm_mod, "STYLE_MAP", getattr(sm_mod, "style_map", STYLE_MAP))
            POVRCHOVE_ZNAKY_RULES = getattr(sm_mod, "POVRCHOVE_ZNAKY_RULES", getattr(sm_mod, "POVRCHOVE_ZNAKY", POVRCHOVE_ZNAKY_RULES))
    except Exception:
        # v krajním případě použijeme výchozí mapu (šedá)
        STYLE_MAP = {"default": {"color": "#808080"}}
        POVRCHOVE_ZNAKY_RULES = {}

# pomocné funkce pro XML (ignorujeme namespace)
def _localname(tag):
    return tag.split('}')[-1] if '}' in tag else tag

def _iter_desc(elem, wanted_local):
    for d in elem.iter():
        if _localname(d.tag) == wanted_local:
            yield d

class XMLJVFImport:
    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = _PLUGIN_DIR

    def initGui(self):
        icon_path = os.path.join(self.plugin_dir, 'icon.png')
        self.action = QAction(QIcon(icon_path), "Import JVF XML", self.iface.mainWindow())
        self.action.triggered.connect(self.run)
        self.iface.addToolBarIcon(self.action)

    def unload(self):
        self.iface.removeToolBarIcon(self.action)

    def run(self):
        fname, _ = QFileDialog.getOpenFileName(
            self.iface.mainWindow(),
            'Otevřít JVF XML soubor',
            '',
            'JVF XML soubory (*.xml *.jvf *.xml.jvf)'
        )
        if fname:
            self.import_jvf_xml(fname)

    # ---------------- GEOMETRIE ----------------
    def parse_poslist(self, text):
        """Robustní parser posList/pos/coordinates/x,y -> list QgsPointXY."""
        if not text:
            return []
        t = str(text).strip()

        # 1) běžný posList (mezery) - rozpozná 2D i 3D
        try:
            nums = list(map(float, re.split(r'\s+', t)))
            dim = 3 if len(nums) % 3 == 0 else 2
            pts = []
            for i in range(0, len(nums), dim):
                pts.append(QgsPointXY(nums[i], nums[i + 1]))
            if pts:
                return pts
        except Exception:
            pass

        # 2) pos ve více řádcích – spojit a interpretovat po dvojicích
        if '\n' in t or re.search(r'\s+', t):
            try:
                nums = list(map(float, re.split(r'\s+', t)))
                pts = []
                for i in range(0, len(nums), 2):
                    pts.append(QgsPointXY(nums[i], nums[i + 1]))
                if pts:
                    return pts
            except Exception:
                pass

        # 3) čárkami oddělené souřadnice "x,y x,y ..." (gml:coordinates varianty)
        if ',' in t:
            pts = []
            parts = re.split(r'\s+', t.replace('\n', ' ').strip())
            for p in parts:
                if ',' in p:
                    try:
                        x_str, y_str = p.split(',', 1)
                        pts.append(QgsPointXY(float(x_str), float(y_str)))
                    except Exception:
                        continue
            if pts:
                return pts

        # fallback: vytáhnout čísla a seskupit po dvojicích
        try:
            nums = list(map(float, re.findall(r'[-+]?\d*\.\d+|[-+]?\d+', t)))
            pts = []
            for i in range(0, len(nums), 2):
                if i + 1 < len(nums):
                    pts.append(QgsPointXY(nums[i], nums[i + 1]))
            return pts
        except Exception:
            return []

    def _extract_geometry_from_record(self, record_elem):
        """Vrátí (QgsGeometry, geometry_type) z jednoho <ZaznamObjektu>."""
        # POINT
        for pt in _iter_desc(record_elem, 'Point'):
            pos_text = None
            for p in _iter_desc(pt, 'pos'):
                if p.text and p.text.strip():
                    pos_text = p.text
                    break
            if pos_text is None:
                for pl in _iter_desc(pt, 'posList'):
                    if pl.text and pl.text.strip():
                        pos_text = pl.text
                        break
            if pos_text is None:
                for anyc in pt.iter():
                    if anyc.text and anyc.text.strip():
                        pos_text = anyc.text
                        break
            pts = self.parse_poslist(pos_text) if pos_text else []
            if pts:
                return QgsGeometry.fromPointXY(pts[0]), "Point"

        # LINESTRING
        for ls in _iter_desc(record_elem, 'LineString'):
            pos_text = None
            for pl in _iter_desc(ls, 'posList'):
                if pl.text and pl.text.strip():
                    pos_text = pl.text
                    break
            if pos_text is None:
                all_pos = [p.text.strip() for p in _iter_desc(ls, 'pos') if p.text and p.text.strip()]
                if all_pos:
                    pos_text = " ".join(all_pos)
            if pos_text is None:
                for co in _iter_desc(ls, 'coordinates'):
                    if co.text and co.text.strip():
                        pos_text = co.text
                        break
            pts = self.parse_poslist(pos_text) if pos_text else []
            if pts:
                return QgsGeometry.fromPolylineXY(pts), "LineString"

        # POLYGON (LinearRing -> posList/pos/coordinates)
        for poly in _iter_desc(record_elem, 'Polygon'):
            rings = []
            for ring in _iter_desc(poly, 'LinearRing'):
                pos_text = None
                for pl in _iter_desc(ring, 'posList'):
                    if pl.text and pl.text.strip():
                        pos_text = pl.text
                        break
                if pos_text is None:
                    all_pos = [p.text.strip() for p in _iter_desc(ring, 'pos') if p.text and p.text.strip()]
                    if all_pos:
                        pos_text = " ".join(all_pos)
                if pos_text is None:
                    for co in _iter_desc(ring, 'coordinates'):
                        if co.text and co.text.strip():
                            pos_text = co.text
                            break
                pts = self.parse_poslist(pos_text) if pos_text else []
                if pts:
                    rings.append(pts)
            if rings:
                return QgsGeometry.fromPolygonXY(rings), "Polygon"

        return None, None

    # ---------------- STYL (POVRCHOVE ZNAKY) ----------------
    def _find_fieldname_case_insensitive(self, layer, name):
        """Najde pole v atributové tabulce bez ohledu na case/znaky."""
        def norm(s):
            s2 = s.lower()
            s2 = re.sub(r'[^a-z0-9]', '', s2)
            return s2
        target = norm(name)
        for f in layer.fields():
            if norm(f.name()) == target:
                return f.name()
        for f in layer.fields():
            if target in norm(f.name()):
                return f.name()
        return None

    def _apply_rule_renderer_for_povrch(self, layer):
        """Rule-based renderer pro PovrchovyZnakTI podle TypPovrchovehoZnakuTI."""
        fieldname = self._find_fieldname_case_insensitive(layer, "TypPovrchovehoZnakuTI")
        if fieldname is None:
            return

        root_rule = QgsRuleBasedRenderer.Rule(None)

        for key, style in POVRCHOVE_ZNAKY_RULES.items():
            label = style.get("label", str(key)) if isinstance(style, dict) else str(key)
            color = style.get("color", "#808080") if isinstance(style, dict) else (style if isinstance(style, str) else "#808080")

            # výraz = číslo bez uvozovek nebo text s uvozovkami
            if re.fullmatch(r'\d+', str(key)):
                expr = f'"{fieldname}" = {int(key)}'
            else:
                expr = f'"{fieldname}" = \'{str(key)}\''

            symbol = QgsSymbol.defaultSymbol(layer.geometryType())
            while symbol.symbolLayerCount() > 0:
                symbol.deleteSymbolLayer(0)

            if layer.geometryType() == QgsWkbTypes.PointGeometry:
                sl = QgsSimpleMarkerSymbolLayer()
                sl.setShape(QgsSimpleMarkerSymbolLayerBase.Triangle)  # ← změní tvar na trojúhelník
                sl.setColor(QColor(color))
                sl.setStrokeColor(QColor("black"))
                sl.setStrokeWidth(0.25)
                sl.setSize(2.0)
                symbol.appendSymbolLayer(sl)
            elif layer.geometryType() == QgsWkbTypes.LineGeometry:
                sl = QgsSimpleLineSymbolLayer()
                sl.setColor(QColor(color))
                sl.setWidth(0.3)
                symbol.appendSymbolLayer(sl)
            else:
                sl = QgsSimpleFillSymbolLayer()
                sl.setColor(QColor(color))
                sl.setStrokeColor(QColor("black"))
                sl.setStrokeWidth(0.25)
                symbol.appendSymbolLayer(sl)

            rule = QgsRuleBasedRenderer.Rule(symbol)
            # QGIS 3.2 / 3.20 safe
            rule.setFilterExpression(expr)
            rule.setLabel(label)
            root_rule.appendChild(rule)

        # (NEPŘIDÁVÁME generické "TRUE" pravidlo, aby nepřebíjelo konkrétní pravidla)
        renderer = QgsRuleBasedRenderer(root_rule)
        layer.setRenderer(renderer)
        layer.triggerRepaint()

    def apply_style(self, layer, geometry_type, obj_type_name):
        """Aplikuje styl pro vrstvu; povrchové znaky přes pravidla."""
        if obj_type_name == "PovrchovyZnakTI":
            self._apply_rule_renderer_for_povrch(layer)
            return

        color = STYLE_MAP.get(obj_type_name, {}).get("color", STYLE_MAP.get("default", {}).get("color", "#808080"))

        symbol = QgsSymbol.defaultSymbol(layer.geometryType())
        while symbol.symbolLayerCount() > 0:
            symbol.deleteSymbolLayer(0)

        if geometry_type == "Point":
            sl = QgsSimpleMarkerSymbolLayer()
            sl.setColor(QColor(color))
            sl.setStrokeColor(QColor("black"))
            sl.setStrokeWidth(0.25)
            sl.setSize(2)
            symbol.appendSymbolLayer(sl)
        elif geometry_type == "LineString":
            sl = QgsSimpleLineSymbolLayer()
            sl.setColor(QColor(color))
            sl.setWidth(0.3)
            symbol.appendSymbolLayer(sl)
        elif geometry_type == "Polygon":
            sl = QgsSimpleFillSymbolLayer()
            sl.setColor(QColor(color))
            sl.setStrokeColor(QColor("black"))
            sl.setStrokeWidth(0.25)
            symbol.appendSymbolLayer(sl)

        layer.renderer().setSymbol(symbol)
        layer.triggerRepaint()

    # ---------------- IMPORT ----------------
    def import_jvf_xml(self, filename):
        try:
            tree = ET.parse(filename)
            root = tree.getroot()
        except Exception as e:
            self.iface.messageBar().pushMessage(f"Chyba při načítání XML: {str(e)}", level=3)
            return

        # najdi element <Data> (ignorujeme namespace)
        data_elem = None
        for elem in root.iter():
            if _localname(elem.tag) == 'Data':
                data_elem = elem
                break
        if data_elem is None:
            self.iface.messageBar().pushMessage("Neplatný JVF soubor - chybí <Data>", level=3)
            return

        # sběr vrstev podle geometrie
        point_layers = []
        line_layers = []
        polygon_layers = []

        for obj_type_elem in list(data_elem):
            obj_type_name = _localname(obj_type_elem.tag)

            zaznamy = [z for z in obj_type_elem.iter() if _localname(z.tag) == 'ZaznamObjektu']
            if not zaznamy:
                continue

            features = []
            all_attributes = []
            geometry_type = None

            for record in zaznamy:
                # atributy
                attrs = {}
                for attr_block in record.iter():
                    if _localname(attr_block.tag) == 'AtributyObjektu':
                        for elem in attr_block.iter():
                            tag_attr = _localname(elem.tag)
                            if elem.text and elem.text.strip():
                                attrs[tag_attr] = elem.text.strip()
                                if tag_attr not in all_attributes:
                                    all_attributes.append(tag_attr)

                # geometrie
                geom, gtype = self._extract_geometry_from_record(record)
                if geom is None:
                    continue

                feat = QgsFeature()
                feat.setGeometry(geom)
                geometry_type = gtype
                feat.setAttributes([attrs.get(a, '') for a in all_attributes])
                features.append(feat)

            if not geometry_type or not features:
                continue

            # vytvoření vrstvy s CRS EPSG:5514
            layer = QgsVectorLayer(f"{geometry_type}?crs=EPSG:5514", obj_type_name, "memory")
            provider = layer.dataProvider()
            provider.addAttributes([QgsField(attr, QVariant.String) for attr in all_attributes])
            layer.updateFields()
            provider.addFeatures(features)
            layer.updateExtents()

            # style
            self.apply_style(layer, geometry_type, obj_type_name)

            # ulož podle geometrie
            if geometry_type == "Point":
                point_layers.append((layer, obj_type_name, len(features)))
            elif geometry_type == "LineString":
                line_layers.append((layer, obj_type_name, len(features)))
            elif geometry_type == "Polygon":
                polygon_layers.append((layer, obj_type_name, len(features)))

        # Přidat vrstvy do projektu tak, aby v legendě byly: body nahoře → linie → plochy dole.
        root = QgsProject.instance().layerTreeRoot()

        # Přidej vrstvy NEVLOŽENÉ do stromu, pak je vložme přesně na index 0 (postupně tak budou v legendě obráceně)
        # 1) přidej všechny bez vložení (addMapLayer with addToLegend=False)
        for lst in (point_layers, line_layers, polygon_layers):
            for layer, _, _ in lst:
                QgsProject.instance().addMapLayer(layer, False)

        # 2) vlož polygony na dno (index 0 opakovaně => výsledkem budou dole)
        for layer, obj_type_name, count in polygon_layers:
            root.insertLayer(0, layer)
            self.iface.messageBar().pushMessage(f"Vrstva '{obj_type_name}' importována ({count} prvků).", level=0)

        # 3) vlož linie nad polygony
        for layer, obj_type_name, count in line_layers:
            root.insertLayer(0, layer)
            self.iface.messageBar().pushMessage(f"Vrstva '{obj_type_name}' importována ({count} prvků).", level=0)

        # 4) vlož body úplně nahoře
        for layer, obj_type_name, count in point_layers:
            root.insertLayer(0, layer)
            self.iface.messageBar().pushMessage(f"Vrstva '{obj_type_name}' importována ({count} prvků).", level=0)
