# -*- coding: utf-8 -*-
from qgis.core import QgsProcessingProvider
from .algorithms.segment_analysis import SegmentAnalysisAlgorithm


class UCAIProvider(QgsProcessingProvider):
    def loadAlgorithms(self):
        self.addAlgorithm(SegmentAnalysisAlgorithm())

    def id(self):
        return "ucai"

    def name(self):
        return "Urban Configurational Accessibility Index (UCAI)"

    def longName(self):
        return self.name()
