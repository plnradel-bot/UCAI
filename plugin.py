# -*- coding: utf-8 -*-
import os

from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction
from qgis.core import QgsApplication

from .provider import UCAIProvider
from .main_dialog import SpaceSyntaxDialog


class UCAIPlugin:
    """QGIS plugin entry point for UCAI."""

    def __init__(self, iface):
        self.iface = iface
        self.provider = None
        self.action = None
        self.toolbar = None
        self.dlg = None
        self.plugin_dir = os.path.dirname(__file__)

    def initGui(self):
        self.provider = UCAIProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)

        icon_path = os.path.join(self.plugin_dir, "icon.png")
        self.action = QAction(QIcon(icon_path), "Urban Configurational Accessibility Index (UCAI)", self.iface.mainWindow())
        self.action.setObjectName("UCAIMain")
        self.action.setToolTip("Analyse street-network Integration, city accessibility profiles and direct two-city comparison")
        self.action.triggered.connect(self.run)

        self.iface.addPluginToVectorMenu("&Urban Configurational Accessibility Index (UCAI)", self.action)
        self.toolbar = self.iface.addToolBar("UCAI")
        self.toolbar.setObjectName("UCAIToolbar")
        self.toolbar.addAction(self.action)

    def unload(self):
        if self.action is not None:
            self.iface.removePluginVectorMenu("&Urban Configurational Accessibility Index (UCAI)", self.action)
            if self.toolbar is not None:
                self.toolbar.removeAction(self.action)
            self.action.deleteLater()
            self.action = None

        if self.toolbar is not None:
            self.toolbar.deleteLater()
            self.toolbar = None

        if self.provider is not None:
            QgsApplication.processingRegistry().removeProvider(self.provider)
            self.provider = None

        if self.dlg is not None:
            self.dlg.close()
            self.dlg = None

    def run(self):
        if self.dlg is None:
            self.dlg = SpaceSyntaxDialog(self.iface)
        self.dlg.show()
        self.dlg.raise_()
        self.dlg.activateWindow()
