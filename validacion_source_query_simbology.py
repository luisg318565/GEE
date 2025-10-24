# -*- coding: utf-8 -*-
# Recorre una carpeta con múltiples MXD y genera un reporte (CSV/XLS) con:
# - nombre del MXD
# - nombre de cada capa visible
# - ruta de la fuente de datos
# - definition query (si existe)
# - campo(s) usado(s) en la simbología
# Además, filtra (omite) los mapas base de Esri/ArcGIS Online para no contaminar el reporte.

from __future__ import print_function, unicode_literals
import arcpy, os, csv, sys, traceback, datetime

arcpy.env.overwriteOutput = True  # permite sobrescribir salidas si ya existen

# ========== EDITA ESTAS RUTAS ==========
# Carpeta con tus MXD (~300). TIP: usa "/" o "\\", evita backslashes con \U en Python 2.
mxd_folder = u"tu-ruta-carpeta-de-mxd"
# Carpeta de salida para CSV/XLS
out_folder = u"tu-ruta-a-carpeta-archivo-CSV"
# ======================================

# Validaciones básicas de carpetas
if not os.path.isdir(mxd_folder):
    raise RuntimeError("La carpeta de MXD no existe: {}".format(mxd_folder))
if not os.path.isdir(out_folder):
    os.makedirs(out_folder)

# Nombres de salida con timestamp para no pisar reportes previos
ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
out_csv = os.path.join(out_folder, "reporte_capas_mxd_{}.csv".format(ts))
out_xls = os.path.join(out_folder, "reporte_capas_mxd_{}.xls".format(ts))

def safe_text(x):
    """Convierte cualquier valor a unicode seguro (UTF-8), evitando errores por tildes/ñ."""
    try:
        if x is None:
            return u""
        if isinstance(x, (str, bytes)):
            if not isinstance(x, unicode):  # Py2: bytes -> unicode
                return x.decode('utf-8', 'ignore')
            return x
        return unicode(x)
    except:
        try:
            return unicode(x)
        except:
            return u""

def iter_visible_layers(mxd):
    """
    Itera TODAS las capas encendidas (checkbox ON) en todos los data frames del MXD.
    Omite capas de grupo (solo devuelve capas de datos).
    """
    try:
        data_frames = arcpy.mapping.ListDataFrames(mxd)
    except:
        data_frames = []
    for df in data_frames:
        for lyr in arcpy.mapping.ListLayers(mxd, "", df):
            # Omite Group Layers (no son capas de datos)
            if getattr(lyr, "isGroupLayer", False):
                continue
            # Solo capas con visibilidad activada (independiente de escala)
            if not getattr(lyr, "visible", False):
                continue
            yield lyr

def get_layer_info(lyr):
    """
    Devuelve:
      - layer_name: nombre largo (incluye jerarquía si la hay)
      - data_source: ruta/URL de la fuente de datos (o BROKEN / no disponible)
      - definition_query: query si existe
    Maneja casos de servicios (WMS/WMTS/REST) y capas rotas.
    """
    # Nombre de la capa (usa longName para conservar jerarquía de grupos)
    try:
        layer_name = safe_text(getattr(lyr, "longName", lyr.name))
    except:
        layer_name = safe_text(lyr.name)

    # Fuente de datos (ruta/URL). Captura robusta con fallback a Describe.
    data_source = u""
    try:
        if getattr(lyr, "isBroken", False):
            data_source = u"BROKEN"  # capa rota: evita intentar leer dataSource
        else:
            if lyr.supports("DATASOURCE"):
                try:
                    data_source = safe_text(lyr.dataSource)
                except:
                    try:
                        d = arcpy.Describe(lyr)
                        if hasattr(d, "catalogPath"):
                            data_source = safe_text(d.catalogPath)
                        else:
                            data_source = u"(sin catalogPath)"
                    except:
                        data_source = u"(no disponible)"
            else:
                # Algunas capas de servicio no “soportan” DATASOURCE
                try:
                    data_source = safe_text(lyr.dataSource)
                except:
                    data_source = u"(no disponible)"
    except:
        data_source = u"(error leyendo dataSource)"

    # Definition Query (si existe)
    definition_query = u""
    try:
        if lyr.supports("DEFINITIONQUERY"):
            dq = getattr(lyr, "definitionQuery", u"")
            if dq:
                definition_query = safe_text(dq)
    except:
        definition_query = u""

    return (layer_name, data_source, definition_query)

def get_symbology_field(lyr):
    """
    Detecta el/los campo(s) usado(s) por la simbología (UNIQUE/GRADUATED/PROPORTIONAL/CHART).
    Devuelve cadena (varios campos separados por ';'). Vacío si no aplica (raster, TIN, etc.).
    """
    try:
        sym = getattr(lyr, "symbology", None)
        if not sym:
            return u""
        stype = safe_text(getattr(lyr, "symbologyType", u"")).upper()

        def first_non_empty(props):
            for p in props:
                try:
                    v = getattr(sym, p, None)
                    if v:
                        return safe_text(v)
                except:
                    pass
            return u""

        if "UNIQUE" in stype:  # Unique Values (1–3 campos)
            fields = first_non_empty(["classField", "valueField"])
            norm = safe_text(getattr(sym, "normalizedByField", u""))
            return fields if not norm else (fields + u" (norm: " + norm + u")")

        if "GRADUATED" in stype or "QUANT" in stype or "PROPORTIONAL" in stype:
            fields = first_non_empty(["valueField", "classificationField", "classField"])
            norm = safe_text(getattr(sym, "normalizedByField", u""))
            return fields if not norm else (fields + u" (norm: " + norm + u")")

        if "CHART" in stype:  # simbología de gráficos (varios campos)
            try:
                flds = getattr(sym, "fields", [])
                if flds:
                    return u";".join([safe_text(f) for f in flds])
            except:
                pass
            return first_non_empty(["valueField", "classField"])

        if "RASTER" in stype or "TIN" in stype or "RGB" in stype:
            return u""  # no hay campo de atributos clásico

        # Fallback genérico
        return first_non_empty(["classField", "valueField", "classificationField"])
    except:
        return u""

def is_esri_basemap(lyr, layer_name, data_source):
    """
    Heurística para omitir mapas base de Esri/ArcGIS Online:
      - Usa flag nativo isBasemapLayer (si existe)
      - Busca palabras clave en nombre/ruta
      - Revisa serviceProperties.URL en dominios arcgisonline/livingatlas
    """
    try:
        if hasattr(lyr, "isBasemapLayer") and lyr.isBasemapLayer:
            return True
    except:
        pass

    text_all = (safe_text(layer_name) + u" " + safe_text(data_source)).lower()
    basemap_keywords = [
        "arcgis online", "server.arcgisonline.com", "services.arcgisonline.com",
        "tiles.arcgis.com", "arcgisonline", "living atlas",
        "world imagery", "world topographic", "world street", "world terrain",
        "world hillshade", "esri", "basemap", "imagery", "streets", "street map",
        "topographic", "shaded relief", "hillshade", "light gray canvas",
        "dark gray canvas", "national geographic", "ocean basemap"
    ]
    if any(k in text_all for k in basemap_keywords):
        return True

    try:
        sp = getattr(lyr, "serviceProperties", None)
        if sp:
            url = safe_text(sp.get("URL", u"")).lower()
            if (".arcgisonline.com" in url) or ("livingatlas.arcgis.com" in url):
                return True
            st = safe_text(sp.get("ServiceType", u"")).lower()
            if ("wmts" in st or "wms" in st or "mapserver" in st) and "arcgisonline" in url:
                return True
    except:
        pass

    return False

# Acumuladores de reporte
# Cada fila: [mxd_name, layer_name, data_source, definition_query, symbology_field]
rows = []
mxd_count = 0
layer_count = 0

print(u"=== Escaneando MXD en: {} ===".format(mxd_folder))

# Recorre todos los MXD de la carpeta
for fname in os.listdir(mxd_folder):
    if not fname.lower().endswith(".mxd"):
        continue
    mxd_path = os.path.join(mxd_folder, fname)
    if not os.path.isfile(mxd_path):
        continue

    mxd_count += 1
    try:
        mxd = arcpy.mapping.MapDocument(mxd_path)
    except Exception as e:
        print(u"[ADVERTENCIA] No se pudo abrir MXD: {} ({})".format(fname, e))
        continue

    try:
        for lyr in iter_visible_layers(mxd):
            layer_name, data_source, definition_query = get_layer_info(lyr)

            # Filtra basemaps de Esri para no incluirlos en el reporte
            try:
                if is_esri_basemap(lyr, layer_name, data_source):
                    continue
            except:
                pass

            sym_field = get_symbology_field(lyr)

            rows.append([safe_text(fname),
                         layer_name,
                         data_source,
                         definition_query,
                         sym_field])
            layer_count += 1
    except Exception as e:
        print(u"[ADVERTENCIA] Error leyendo capas en {}: {}".format(fname, e))
    finally:
        try:
            del mxd  # libera recursos
        except:
            pass

print(u"MXD leídos: {} | Capas visibles listadas (sin basemaps): {}".format(mxd_count, layer_count))

# --- Exportar CSV (siempre robusto) ---
header = ["mxd_name", "layer_name", "data_source", "definition_query", "symbology_field"]
with open(out_csv, "wb") as f:
    writer = csv.writer(f)
    writer.writerow([h.encode("utf-8") for h in header])  # cabecera
    for r in rows:
        writer.writerow([safe_text(x).encode("utf-8") for x in r])

print(u"✔ CSV creado: {}".format(out_csv))

# --- Intentar también XLS mediante Table To Excel (si está disponible) ---
def table_to_excel_from_rows(rows, header, out_xls):
    try:
        in_mem = r"in_memory\reporte_capas_mxd"
        if arcpy.Exists(in_mem):
            arcpy.Delete_management(in_mem)
        arcpy.CreateTable_management("in_memory", "reporte_capas_mxd")

        # Estructura de la tabla temporal (longitudes generosas)
        arcpy.AddField_management(in_mem, "mxd_name", "TEXT", field_length=255)
        arcpy.AddField_management(in_mem, "layer_name", "TEXT", field_length=512)
        arcpy.AddField_management(in_mem, "data_source", "TEXT", field_length=1024)
        arcpy.AddField_management(in_mem, "definition_query", "TEXT", field_length=4000)
        arcpy.AddField_management(in_mem, "symbology_field", "TEXT", field_length=512)

        with arcpy.da.InsertCursor(in_mem,
                                   ["mxd_name", "layer_name", "data_source", "definition_query", "symbology_field"]) as ic:
            for r in rows:
                ic.insertRow([safe_text(r[0]),
                              safe_text(r[1]),
                              safe_text(r[2]),
                              safe_text(r[3]),
                              safe_text(r[4])])

        # Algunas instalaciones exponen la herramienta como TableToExcel_conversion
        if hasattr(arcpy, "TableToExcel_conversion"):
            arcpy.TableToExcel_conversion(in_mem, out_xls)
        else:
            arcpy.conversion.TableToExcel(in_mem, out_xls)

        arcpy.Delete_management(in_mem)
        return True
    except arcpy.ExecuteError:
        print(u"[ADVERTENCIA] Table To Excel falló: {}".format(arcpy.GetMessages(2)))
        try:
            arcpy.Delete_management(in_mem)
        except:
            pass
        return False
    except Exception as e:
        print(u"[ADVERTENCIA] No fue posible crear XLS: {}".format(e))
        try:
            arcpy.Delete_management(in_mem)
        except:
            pass
        return False

if table_to_excel_from_rows(rows, header, out_xls):
    print(u" XLS creado: {}".format(out_xls))
else:
    print(u" No se pudo crear .XLS automáticamente. Queda el CSV.")

print(u"FIN.")
