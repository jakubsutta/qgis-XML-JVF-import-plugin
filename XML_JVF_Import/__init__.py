# -*- coding: utf-8 -*-
def classFactory(iface):
    from .xml_jvf_import import XMLJVFImport
    return XMLJVFImport(iface)
