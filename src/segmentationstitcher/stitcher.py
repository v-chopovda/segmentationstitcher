"""
Interface for stitching segmentation data from and calculating transformations between adjacent image blocks.
"""

from cmlibs.maths.vectorops import add
from cmlibs.utils.zinc.general import HierarchicalChangeManager
from cmlibs.utils.zinc.field import findOrCreateFieldCoordinates, findOrCreateFieldStoredString, findOrCreateFieldGroup
from cmlibs.zinc.context import Context
from cmlibs.zinc.field import Field, FieldGroup
from cmlibs.zinc.element import Element, Elementbasis
from cmlibs.zinc.node import Node
from segmentationstitcher.connection import Connection
from segmentationstitcher.segment import Segment
from segmentationstitcher.annotation import region_get_annotations

import copy
from pathlib import Path


class Stitcher:
    """
    Interface for stitching segmentation data from and calculating transformations between adjacent image blocks.
    """

    def __init__(self, segmentation_file_names: list, network_group1_keywords, network_group2_keywords):
        """
        :param segmentation_file_names: List of filenames containing raw segmentations in Zinc format.
        :param network_group1_keywords: List of keywords. Segmented networks annotated with any of these keywords are
        initially assigned to network group 1, allowing them to be stitched together.
        :param network_group2_keywords: List of keywords. Segmented networks annotated with any of these keywords are
        initially assigned to network group 2, allowing them to be stitched together.
        """
        self._context = Context("Segmentation Stitcher")
        self._root_region = self._context.getDefaultRegion()
        self._annotations = []
        self._network_group1_keywords = copy.deepcopy(network_group1_keywords)
        self._network_group2_keywords = copy.deepcopy(network_group2_keywords)
        self._term_keywords = ['fma:', 'fma_', 'ilx:', 'ilx_', 'uberon:', 'uberon_']
        self._segments = []
        self._connections = []
        self._version = 1  # increment when new settings added to migrate older serialised settings
        for segmentation_file_name in segmentation_file_names:
            name = Path(segmentation_file_name).stem
            segment = Segment(name, segmentation_file_name, self._root_region)
            self._segments.append(segment)
            segment_annotations = region_get_annotations(
                segment.get_raw_region(), self._network_group1_keywords, self._network_group2_keywords,
                self._term_keywords)
            for segment_annotation in segment_annotations:
                name = segment_annotation.get_name()
                term = segment_annotation.get_term()
                index = 0
                for annotation in self._annotations:
                    if (annotation.get_name() == name) and (annotation.get_term() == term):
                        # print("Found annotation name", name, "term", term)
                        break  # exists already
                    if name > annotation.get_name():
                        index += 1
                else:
                    # print("Add annoation name", name, "term", term, "dim", segment_annotation.get_dimension(),
                    #       "category", segment_annotation.get_category())
                    self._annotations.insert(index, segment_annotation)
        with HierarchicalChangeManager(self._root_region):
            for segment in self._segments:
                segment.reset_annotation_category_groups(self._annotations)
        for annotation in self._annotations:
            annotation.set_category_change_callback(self._annotation_change)

    def decode_settings(self, settings_in: dict):
        """
        Update stitcher settings from dictionary of serialised settings.
        :param settings_in: Dictionary of settings as produced by encode_settings().
        """
        assert settings_in.get("annotations") and settings_in.get("segments") and settings_in.get("version"), \
            "Stitcher.decode_settings: Invalid settings dictionary"
        # settings_version = settings_in["version"]

        # update annotations and warn about differences
        processed_count = 0
        for annotation_settings in settings_in["annotations"]:
            name = annotation_settings["name"]
            term = annotation_settings["term"]
            for annotation in self._annotations:
                if (annotation.get_name() == name) and (annotation.get_term() == term):
                    annotation.decode_settings(annotation_settings)
                    processed_count += 1
                    break
            else:
                print("WARNING: Segmentation Stitcher.  Annotation with name", name, "term", term,
                      "in settings not found; ignoring. Have input files changed?")
        if processed_count != len(self._annotations):
            for annotation in self._annotations:
                name = annotation.get_name()
                term = annotation.get_term()
                for annotation_settings in settings_in["annotations"]:
                    if (annotation_settings["name"] == name) and (annotation_settings["term"] == term):
                        break
                else:
                    print("WARNING: Segmentation Stitcher.  Annotation with name", name, "term", term,
                          "not found in settings; using defaults. Have input files changed?")

        # update segment settings and warn about differences
        processed_count = 0
        for segment_settings in settings_in["segments"]:
            name = segment_settings["name"]
            for segment in self._segments:
                if segment.get_name() == name:
                    segment.decode_settings(segment_settings)
                    processed_count += 1
                    break
            else:
                print("WARNING: Segmentation Stitcher.  Segment with name", name,
                      "in settings not found; ignoring. Have input files changed?")
        if processed_count != len(self._segments):
            for segment in self._segments:
                name = segment.get_name()
                for segment_settings in settings_in["segments"]:
                    if segment_settings["name"] == name:
                        break
                else:
                    print("WARNING: Segmentation Stitcher.  Segment with name", name,
                          "not found in settings; using defaults. Have input files changed?")
        with HierarchicalChangeManager(self._root_region):
            for segment in self._segments:
                segment.reset_annotation_category_groups(self._annotations)

        # create connections from stitcher settings' connection serialisations
        assert len(self._connections) == 0, "Cannot decode connections after any exist"
        for connection_settings in settings_in["connections"]:
            connection_segments = []
            for segment_name in connection_settings["segments"]:
                for segment in self._segments:
                    if segment.get_name() == segment_name:
                        connection_segments.append(segment)
                        break
                else:
                    print("WARNING: Segmentation Stitcher.  Segment with name", segment_name,
                          "in connection settings not found; ignoring. Have input files changed?")
            if len(connection_segments) >= 2:
                connection = self.create_connection(connection_segments)
                connection.decode_settings(connection_settings)


    def encode_settings(self) -> dict:
        """
        :return: Dictionary of Stitcher settings ready to serialise to JSON.
        """
        settings = {
            "annotations": [annotation.encode_settings() for annotation in self._annotations],
            "connections": [connection.encode_settings() for connection in self._connections],
            "segments": [segment.encode_settings() for segment in self._segments],
            "version": self._version
        }
        return settings

    def _annotation_change(self, annotation, old_category):
        """
        Callback from annotation that its category has changed.
        Update segment category groups.
        :param annotation: Annotation that has changed category.
        :param old_category: The old category to remove segmentations with annotation from.
        """
        with HierarchicalChangeManager(self._root_region):
            for segment in self._segments:
                segment.update_annotation_category(annotation, old_category)

    def get_annotations(self):
        return self._annotations

    def create_connection(self, segments):
        """
        :param segments: List of 2 Stitcher Segment objects to connect.
        :return: Connection object or None if invalid segments or connection between segments already exists
        """
        if len(segments) != 2:
            print("Only supports connections between 2 segments")
            return None
        for connection in self._connections:
            if all(segment in connection.get_segments() for segment in segments):
                print("Stitcher.create_connection:  Already have a connection between segments")
                return None
        connection = Connection(segments, self._root_region)
        self._connections.append(connection)
        return connection

    def get_connections(self):
        return self._connections

    def remove_connection(self, connection):
        self._connections.remove(connection)

    def get_context(self):
        return self._context

    def get_root_region(self):
        return self._root_region

    def get_segments(self):
        return self._segments

    def get_version(self):
        return self._version

    def write_output_segmentation_file(self, file_name):
        pass

    def write_output_vagus_segmentation_file_valerie(self, file_name):
        """
        Writes out exf file.
        At the moment only considers annotation groups from NETWORK_GROUP_1 category (trunk, branch centroid groups)
        """

        with HierarchicalChangeManager(self._root_region):
            # region to write all stitched data into
            stitched_region = self._root_region.createRegion()
            fieldmodule = stitched_region.getFieldmodule()
            fieldcache = fieldmodule.createFieldcache()
            coordinates = findOrCreateFieldCoordinates(fieldmodule).castFiniteElement()

            # nodes settings
            nodes = fieldmodule.findNodesetByFieldDomainType(Field.DOMAIN_TYPE_NODES)
            nodetemplate = nodes.createNodetemplate()
            nodetemplate.defineField(coordinates)
            nodetemplate.setValueNumberOfVersions(coordinates, -1, Node.VALUE_LABEL_VALUE, 1)

            # elements settings
            mesh1d = fieldmodule.findMeshByDimension(1)
            linear_basis = fieldmodule.createElementbasis(1, Elementbasis.FUNCTION_TYPE_LINEAR_LAGRANGE)
            eft = mesh1d.createElementfieldtemplate(linear_basis)
            elementtemplate = mesh1d.createElementtemplate()
            elementtemplate.setElementShapeType(Element.SHAPE_TYPE_LINE)
            elementtemplate.defineField(coordinates, -1, eft)

            # markers settings
            datapoints = fieldmodule.findNodesetByFieldDomainType(Field.DOMAIN_TYPE_DATAPOINTS)
            marker_fieldgroup = findOrCreateFieldGroup(fieldmodule, 'marker')
            marker_nodesetgroup = marker_fieldgroup.createNodesetGroup(datapoints)
            marker_names = findOrCreateFieldStoredString(fieldmodule, name="marker_name")
            dnodetemplate = datapoints.createNodetemplate()
            dnodetemplate.defineField(coordinates)
            dnodetemplate.setValueNumberOfVersions(coordinates, -1, Node.VALUE_LABEL_VALUE, 1)
            dnodetemplate.defineField(marker_names)

            nodeIdentifier = 1
            elementIdentifier = 1
            markerNodeIdentifier = 1

            nodes_per_field_group = {}
            for segment_id, segment in enumerate(self.get_segments()):
                segment_region = segment.get_raw_region()
                segment_fieldmodule = segment_region.getFieldmodule()
                segment_fieldcache = segment_fieldmodule.createFieldcache()
                segment_coordinates = segment_fieldmodule.findFieldByName("coordinates").castFiniteElement()
                segment_nodes = segment_fieldmodule.findNodesetByFieldDomainType(Field.DOMAIN_TYPE_NODES)

                # annotations sorting:
                segment_annotations = region_get_annotations(segment_region, ['vagus', 'nerve', 'trunk', 'branch'], [], [])
                # network group 1: trunk group, then everything that is 'nerve' or 'branch'
                # for now ignore other annotation groups
                trunk_keywords = ['left vagus nerve', 'right vagus nerve',
                                  'left vagus X nerve trunk', 'right vagus X nerve trunk']
                trunk_group_name = [annotation.get_name() for annotation in segment_annotations if
                                    annotation.get_name() in trunk_keywords][0]
                segment_annotations = [annotation.get_name() for annotation in segment_annotations if
                                       annotation.get_name() not in ['marker', trunk_group_name]]
                segment_annotations.sort(reverse=True)
                segment_annotations.insert(0, trunk_group_name)

                if segment_id > 0:
                    # group names that need to be stitched & number of elements that needs to be added in between
                    groups_to_connect = list(set(segment_annotations) & set(nodes_per_field_group.keys()))
                    print('Common groups:', groups_to_connect)

                node_map = {}  # old node -> new node (within one segment)
                # nodes & elements
                for annotation in segment_annotations:
                    if annotation not in nodes_per_field_group.keys():
                        # new annotation group
                        nodes_per_field_group[annotation] = []

                    segment_field_group = findOrCreateFieldGroup(segment_fieldmodule, annotation)
                    segment_field_nodes = segment_field_group.getNodesetGroup(segment_nodes)

                    field_group = findOrCreateFieldGroup(fieldmodule, annotation)
                    field_group.setSubelementHandlingMode(FieldGroup.SUBELEMENT_HANDLING_MODE_FULL)
                    mesh_group = field_group.getOrCreateMeshGroup(mesh1d)

                    segment_node_iter = segment_field_nodes.createNodeiterator()
                    segment_node = segment_node_iter.next()
                    while segment_node.isValid():
                        segment_node_id = segment_node.getIdentifier()
                        if segment_node_id in node_map.keys():
                            # node already exist
                            nodes_per_field_group[annotation].append(node_map[segment_node_id])
                            segment_node = segment_node_iter.next()
                            continue

                        segment_fieldcache.setNode(segment_node)
                        _, xyz = segment_coordinates.getNodeParameters(segment_fieldcache, -1, Node.VALUE_LABEL_VALUE, 1, 3)
                        xyz = add(xyz, segment.get_translation())  # apply translation to node

                        node = nodes.createNode(nodeIdentifier, nodetemplate)
                        fieldcache.setNode(node)
                        coordinates.setNodeParameters(fieldcache, -1, Node.VALUE_LABEL_VALUE, 1, xyz)
                        if nodeIdentifier > 1:
                            nids = [nodes_per_field_group[annotation][-1], nodeIdentifier]
                            element = mesh1d.createElement(elementIdentifier, elementtemplate)
                            element.setNodesByIdentifier(eft, nids)
                            mesh_group.addElement(element)
                            elementIdentifier += 1

                        node_map[segment_node_id] = nodeIdentifier
                        nodes_per_field_group[annotation].append(nodeIdentifier)
                        nodeIdentifier += 1
                        segment_node = segment_node_iter.next()

                # markers
                segment_markers = segment_fieldmodule.findNodesetByFieldDomainType(Field.DOMAIN_TYPE_DATAPOINTS)
                segment_marker_name_field = segment_fieldmodule.findFieldByName("marker_name")
                segment_marker_group = findOrCreateFieldGroup(segment_fieldmodule, 'marker')
                segment_marker_nodes = segment_marker_group.getNodesetGroup(segment_markers)

                segment_marker_node_iter = segment_marker_nodes.createNodeiterator()
                segment_marker_node = segment_marker_node_iter.next()
                while segment_marker_node.isValid():
                    segment_fieldcache.setNode(segment_marker_node)
                    _, marker_xyz = segment_coordinates.evaluateReal(segment_fieldcache, 3)
                    marker_name = segment_marker_name_field.evaluateString(segment_fieldcache)
                    marker_xyz = add(marker_xyz, segment.get_translation())  # apply translation to marker

                    node = datapoints.createNode(markerNodeIdentifier, dnodetemplate)
                    fieldcache.setNode(node)
                    coordinates.setNodeParameters(fieldcache, -1, Node.VALUE_LABEL_VALUE, 1, marker_xyz)
                    marker_names.assignString(fieldcache, marker_name)
                    marker_nodesetgroup.addNode(node)
                    markerNodeIdentifier += 1

                    segment_marker_node = segment_marker_node_iter.next()

            # write all data in one exf file
            sir = stitched_region.createStreaminformationRegion()
            srf = sir.createStreamresourceFile(file_name)
            stitched_region.write(sir)



