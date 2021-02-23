# Copyright (C) 2020 FireEye, Inc. All Rights Reserved.
# Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
# You may obtain a copy of the License at: [package root]/LICENSE.txt
# Unless required by applicable law or agreed to in writing, software distributed under the License
#  is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and limitations under the License.
import re
from collections import Counter

import idc
from PyQt5 import QtGui, QtCore, QtWidgets

import capa.rules
import capa.engine
import capa.ida.helpers
import capa.features.basicblock
from capa.ida.plugin.item import CapaExplorerFunctionItem
from capa.ida.plugin.model import CapaExplorerDataModel

MAX_SECTION_SIZE = 750

# default colors used in views
COLOR_GREEN_RGB = (79, 121, 66)
COLOR_BLUE_RGB = (37, 147, 215)


def calc_level_by_indent(line, prev_level=0):
    """ """
    if not len(line.strip()):
        # blank line, which may occur for comments so we simply use the last level
        return prev_level
    stripped = line.lstrip()
    if stripped.startswith("description"):
        # need to adjust two spaces when encountering string description
        line = line[2:]
    # calc line level based on preceding whitespace
    return len(line) - len(stripped)


def parse_feature_for_node(feature):
    """ """
    description = ""
    comment = ""

    if feature.startswith("- count"):
        # count is weird, we need to handle special
        # first, we need to grab the comment, if exists
        # next, we need to check for an embedded description
        feature, _, comment = feature.partition("#")
        m = re.search(r"- count\(([a-zA-Z]+)\((.+)\s+=\s+(.+)\)\):\s*(.+)", feature)
        if m:
            # reconstruct count without description
            feature, value, description, count = m.groups()
            feature = "- count(%s(%s)): %s" % (feature, value, count)
    elif not feature.startswith("#"):
        feature, _, comment = feature.partition("#")
        feature, _, description = feature.partition("=")

    return map(lambda o: o.strip(), (feature, description, comment))


def parse_node_for_feature(feature, description, comment, depth):
    """ """
    depth = (depth * 2) + 4
    display = ""

    if feature.startswith("#"):
        display += "%s%s\n" % (" " * depth, feature)
    elif description:
        if feature.startswith(("- and", "- or", "- optional", "- basic block", "- not")):
            display += "%s%s" % (" " * depth, feature)
            if comment:
                display += " # %s" % comment
            display += "\n%s- description: %s\n" % (" " * (depth + 2), description)
        elif feature.startswith("- string"):
            display += "%s%s" % (" " * depth, feature)
            if comment:
                display += " # %s" % comment
            display += "\n%sdescription: %s\n" % (" " * (depth + 2), description)
        elif feature.startswith("- count"):
            # count is weird, we need to format description based on feature type, so we parse with regex
            # assume format - count(<feature_name>(<feature_value>)): <count>
            m = re.search(r"- count\(([a-zA-Z]+)\((.+)\)\): (.+)", feature)
            if m:
                name, value, count = m.groups()
                if name in ("string",):
                    display += "%s%s" % (" " * depth, feature)
                    if comment:
                        display += " # %s" % comment
                    display += "\n%sdescription: %s\n" % (" " * (depth + 2), description)
                else:
                    display += "%s- count(%s(%s = %s)): %s" % (
                        " " * depth,
                        name,
                        value,
                        description,
                        count,
                    )
                    if comment:
                        display += " # %s\n" % comment
        else:
            display += "%s%s = %s" % (" " * depth, feature, description)
            if comment:
                display += " # %s\n" % comment
    else:
        display += "%s%s" % (" " * depth, feature)
        if comment:
            display += " # %s\n" % comment

    return display if display.endswith("\n") else display + "\n"


def yaml_to_nodes(s):
    level = 0
    for line in s.splitlines():
        feature, description, comment = parse_feature_for_node(line.strip())

        o = QtWidgets.QTreeWidgetItem(None)

        # set node attributes
        setattr(o, "capa_level", calc_level_by_indent(line, level))

        if feature.startswith(("- and:", "- or:", "- not:", "- basic block:", "- optional:")):
            setattr(o, "capa_type", CapaExplorerRulgenEditor.get_node_type_expression())
        elif feature.startswith("#"):
            setattr(o, "capa_type", CapaExplorerRulgenEditor.get_node_type_comment())
        else:
            setattr(o, "capa_type", CapaExplorerRulgenEditor.get_node_type_feature())

        # set node text
        for (i, v) in enumerate((feature, description, comment)):
            o.setText(i, v)

        yield o


def iterate_tree(o):
    """ """
    itr = QtWidgets.QTreeWidgetItemIterator(o)
    while itr.value():
        yield itr.value()
        itr += 1


def calc_item_depth(o):
    """ """
    depth = 0
    while True:
        if not o.parent():
            break
        depth += 1
        o = o.parent()
    return depth


def build_action(o, display, data, slot):
    """ """
    action = QtWidgets.QAction(display, o)

    action.setData(data)
    action.triggered.connect(lambda checked: slot(action))

    return action


def build_context_menu(o, actions):
    """ """
    menu = QtWidgets.QMenu()

    for action in actions:
        if isinstance(action, QtWidgets.QMenu):
            menu.addMenu(action)
        else:
            menu.addAction(build_action(o, *action))

    return menu


class CapaExplorerRulgenPreview(QtWidgets.QTextEdit):
    def __init__(self, parent=None):
        """ """
        super(CapaExplorerRulgenPreview, self).__init__(parent)

        self.setFont(QtGui.QFont("Courier", weight=QtGui.QFont.Bold))
        self.setLineWrapMode(QtWidgets.QTextEdit.NoWrap)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)

    def reset_view(self):
        """ """
        self.clear()

    def load_preview_meta(self, ea, author, scope):
        """ """
        metadata_default = [
            "# generated using capa explorer for IDA Pro",
            "rule:",
            "  meta:",
            "    name: <insert_name>",
            "    namespace: <insert_namespace>",
            "    author: %s" % author,
            "    scope: %s" % scope,
            "    references: <insert_references>",
            "    examples:",
            "      - %s:0x%X" % (capa.ida.helpers.get_file_md5().upper(), ea)
            if ea
            else "      - %s" % (capa.ida.helpers.get_file_md5().upper()),
            "  features:",
        ]
        self.setText("\n".join(metadata_default))

    def keyPressEvent(self, e):
        """ """
        if e.key() == QtCore.Qt.Key_Tab:
            self.insertPlainText(" " * 2)
        else:
            super(CapaExplorerRulgenPreview, self).keyPressEvent(e)


class CapaExplorerRulgenEditor(QtWidgets.QTreeWidget):

    updated = QtCore.pyqtSignal()

    def __init__(self, preview, parent=None):
        """ """
        super(CapaExplorerRulgenEditor, self).__init__(parent)

        self.preview = preview

        self.setHeaderLabels(["Feature", "Description", "Comment"])
        self.header().setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        self.header().setStretchLastSection(False)
        self.setExpandsOnDoubleClick(False)
        self.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.setStyleSheet("QTreeView::item {padding-right: 15 px;padding-bottom: 2 px;}")

        # enable drag and drop
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QtWidgets.QAbstractItemView.InternalMove)

        # connect slots
        self.itemChanged.connect(self.slot_item_changed)
        self.customContextMenuRequested.connect(self.slot_custom_context_menu_requested)
        self.itemDoubleClicked.connect(self.slot_item_double_clicked)

        self.root = None
        self.reset_view()

        self.is_editing = False

    @staticmethod
    def get_column_feature_index():
        """ """
        return 0

    @staticmethod
    def get_column_description_index():
        """ """
        return 1

    @staticmethod
    def get_column_comment_index():
        """ """
        return 2

    @staticmethod
    def get_node_type_expression():
        """ """
        return 0

    @staticmethod
    def get_node_type_feature():
        """ """
        return 1

    @staticmethod
    def get_node_type_comment():
        """ """
        return 2

    def dragMoveEvent(self, e):
        """ """
        super(CapaExplorerRulgenEditor, self).dragMoveEvent(e)

    def dragEventEnter(self, e):
        """ """
        super(CapaExplorerRulgenEditor, self).dragEventEnter(e)

    def dropEvent(self, e):
        """ """
        if not self.indexAt(e.pos()).isValid():
            return

        super(CapaExplorerRulgenEditor, self).dropEvent(e)

        # self.prune_expressions()
        self.update_preview()
        self.expandAll()

    def reset_view(self):
        """ """
        self.root = None
        self.clear()

    def slot_item_changed(self, item, column):
        """ """
        if self.is_editing:
            self.update_preview()
            self.is_editing = False

    def slot_remove_selected(self, action):
        """ """
        for o in self.selectedItems():
            if o == self.root:
                self.takeTopLevelItem(self.indexOfTopLevelItem(o))
                self.root = None
                continue
            o.parent().removeChild(o)

    def slot_nest_features(self, action):
        """ """
        # create a new parent under root node, by default; new node added last position in tree
        new_parent = self.new_expression_node(self.root, (action.data()[0], ""))

        for o in self.get_features(selected=True):
            # take child from its parent by index, add to new parent
            new_parent.addChild(o.parent().takeChild(o.parent().indexOfChild(o)))

        # ensure new parent expanded
        new_parent.setExpanded(True)

    def slot_edit_expression(self, action):
        """ """
        expression, o = action.data()
        o.setText(CapaExplorerRulgenEditor.get_column_feature_index(), expression)

    def slot_clear_all(self, action):
        """ """
        self.reset_view()

    def slot_custom_context_menu_requested(self, pos):
        """ """
        if not self.indexAt(pos).isValid():
            # user selected invalid index
            self.load_custom_context_menu_invalid_index(pos)
        elif self.itemAt(pos).capa_type == CapaExplorerRulgenEditor.get_node_type_expression():
            # user selected expression node
            self.load_custom_context_menu_expression(pos)
        else:
            # user selected feature node
            self.load_custom_context_menu_feature(pos)

        self.update_preview()

    def slot_item_double_clicked(self, o, column):
        """ """
        if column in (
            CapaExplorerRulgenEditor.get_column_comment_index(),
            CapaExplorerRulgenEditor.get_column_description_index(),
        ):
            o.setFlags(o.flags() | QtCore.Qt.ItemIsEditable)
            self.editItem(o, column)
            o.setFlags(o.flags() & ~QtCore.Qt.ItemIsEditable)
            self.is_editing = True

    def update_preview(self):
        """ """
        rule_text = self.preview.toPlainText()

        if -1 != rule_text.find("features:"):
            rule_text = rule_text[: rule_text.find("features:") + len("features:")]
            rule_text += "\n"
        else:
            rule_text = rule_text.rstrip()
            rule_text += "\n  features:\n"

        for o in iterate_tree(self):
            feature, description, comment = map(lambda o: o.strip(), tuple(o.text(i) for i in range(3)))
            rule_text += parse_node_for_feature(feature, description, comment, calc_item_depth(o))

        # FIXME we avoid circular update by disabling signals when updating
        # the preview. Preferably we would refactor the code to avoid this
        # in the first place
        self.preview.blockSignals(True)
        self.preview.setPlainText(rule_text)
        self.preview.blockSignals(False)

        # emit signal so views can update
        self.updated.emit()

    def load_custom_context_menu_invalid_index(self, pos):
        """ """
        actions = (("Remove all", (), self.slot_clear_all),)

        menu = build_context_menu(self.parent(), actions)
        menu.exec_(self.viewport().mapToGlobal(pos))

    def load_custom_context_menu_feature(self, pos):
        """ """
        actions = (("Remove selection", (), self.slot_remove_selected),)

        sub_actions = (
            ("and", ("- and:",), self.slot_nest_features),
            ("or", ("- or:",), self.slot_nest_features),
            ("not", ("- not:",), self.slot_nest_features),
            ("optional", ("- optional:",), self.slot_nest_features),
            ("basic block", ("- basic block:",), self.slot_nest_features),
        )

        # build submenu with modify actions
        sub_menu = build_context_menu(self.parent(), sub_actions)
        sub_menu.setTitle("Nest feature%s" % ("" if len(tuple(self.get_features(selected=True))) == 1 else "s"))

        # build main menu with submenu + main actions
        menu = build_context_menu(self.parent(), (sub_menu,) + actions)

        menu.exec_(self.viewport().mapToGlobal(pos))

    def load_custom_context_menu_expression(self, pos):
        """ """
        actions = (("Remove expression", (), self.slot_remove_selected),)

        sub_actions = (
            ("and", ("- and:", self.itemAt(pos)), self.slot_edit_expression),
            ("or", ("- or:", self.itemAt(pos)), self.slot_edit_expression),
            ("not", ("- not:", self.itemAt(pos)), self.slot_edit_expression),
            ("optional", ("- optional:", self.itemAt(pos)), self.slot_edit_expression),
            ("basic block", ("- basic block:", self.itemAt(pos)), self.slot_edit_expression),
        )

        # build submenu with modify actions
        sub_menu = build_context_menu(self.parent(), sub_actions)
        sub_menu.setTitle("Modify")

        # build main menu with submenu + main actions
        menu = build_context_menu(self.parent(), (sub_menu,) + actions)

        menu.exec_(self.viewport().mapToGlobal(pos))

    def style_expression_node(self, o):
        """ """
        font = QtGui.QFont()
        font.setBold(True)

        o.setFont(CapaExplorerRulgenEditor.get_column_feature_index(), font)

    def style_feature_node(self, o):
        """ """
        font = QtGui.QFont()
        brush = QtGui.QBrush()

        font.setFamily("Courier")
        font.setWeight(QtGui.QFont.Medium)
        brush.setColor(QtGui.QColor(*COLOR_GREEN_RGB))

        o.setFont(CapaExplorerRulgenEditor.get_column_feature_index(), font)
        o.setForeground(CapaExplorerRulgenEditor.get_column_feature_index(), brush)

    def style_comment_node(self, o):
        """ """
        font = QtGui.QFont()
        font.setBold(True)
        font.setFamily("Courier")

        o.setFont(CapaExplorerRulgenEditor.get_column_feature_index(), font)

    def set_expression_node(self, o):
        """ """
        setattr(o, "capa_type", CapaExplorerRulgenEditor.get_node_type_expression())
        self.style_expression_node(o)

    def set_feature_node(self, o):
        """ """
        setattr(o, "capa_type", CapaExplorerRulgenEditor.get_node_type_feature())
        o.setFlags(o.flags() & ~QtCore.Qt.ItemIsDropEnabled)
        self.style_feature_node(o)

    def set_comment_node(self, o):
        """ """
        setattr(o, "capa_type", CapaExplorerRulgenEditor.get_node_type_comment())
        o.setFlags(o.flags() & ~QtCore.Qt.ItemIsDropEnabled)

        self.style_comment_node(o)

    def new_expression_node(self, parent, values=()):
        """ """
        o = QtWidgets.QTreeWidgetItem(parent)
        self.set_expression_node(o)
        for (i, v) in enumerate(values):
            o.setText(i, v)
        return o

    def new_feature_node(self, parent, values=()):
        """ """
        o = QtWidgets.QTreeWidgetItem(parent)
        self.set_feature_node(o)
        for (i, v) in enumerate(values):
            o.setText(i, v)
        return o

    def new_comment_node(self, parent, values=()):
        """ """
        o = QtWidgets.QTreeWidgetItem(parent)
        self.set_comment_node(o)
        for (i, v) in enumerate(values):
            o.setText(i, v)
        return o

    def update_features(self, features):
        """ """
        if not self.root:
            # root node does not exist, create default node, set expanded
            self.root = self.new_expression_node(self, ("- or:", ""))

        # build feature counts
        counted = list(zip(Counter(features).keys(), Counter(features).values()))

        # single features
        for (k, v) in filter(lambda t: t[1] == 1, counted):
            self.new_feature_node(self.root, ("- %s: %s" % (k.name.lower(), k.get_value_str()), ""))

        # n > 1 features
        for (k, v) in filter(lambda t: t[1] > 1, counted):
            self.new_feature_node(self.root, ("- count(%s): %d" % (str(k), v), ""))

        self.expandAll()
        self.update_preview()

    def load_features_from_yaml(self, rule_text, update_preview=False):
        """ """

        def add_node(parent, node):
            if node.text(0).startswith("description:"):
                if parent.childCount():
                    parent.child(parent.childCount() - 1).setText(1, node.text(0).lstrip("description:").lstrip())
                else:
                    parent.setText(1, node.text(0).lstrip("description:").lstrip())
            elif node.text(0).startswith("- description:"):
                parent.setText(1, node.text(0).lstrip("- description:").lstrip())
            else:
                parent.addChild(node)

        def build(parent, nodes):
            if nodes:
                child_lvl = nodes[0].capa_level
                while nodes:
                    node = nodes.pop(0)
                    if node.capa_level == child_lvl:
                        add_node(parent, node)
                    elif node.capa_level > child_lvl:
                        nodes.insert(0, node)
                        build(parent.child(parent.childCount() - 1), nodes)
                    else:
                        parent = parent.parent() if parent.parent() else parent
                        add_node(parent, node)

        self.reset_view()

        # check for lack of features block
        if -1 == rule_text.find("features:"):
            return

        rule_features = rule_text[rule_text.find("features:") + len("features:") :].strip()
        rule_nodes = list(yaml_to_nodes(rule_features))

        # check for lack of nodes
        if not rule_nodes:
            return

        for o in rule_nodes:
            (self.set_expression_node, self.set_feature_node, self.set_comment_node)[o.capa_type](o)

        self.root = rule_nodes.pop(0)
        self.addTopLevelItem(self.root)

        if update_preview:
            self.preview.blockSignals(True)
            self.preview.setPlainText(rule_text)
            self.preview.blockSignals(False)

        build(self.root, rule_nodes)

        self.expandAll()

    def get_features(self, selected=False, ignore=()):
        """ """
        for feature in filter(
            lambda o: o.capa_type
            in (CapaExplorerRulgenEditor.get_node_type_feature(), CapaExplorerRulgenEditor.get_node_type_comment()),
            tuple(iterate_tree(self)),
        ):
            if feature in ignore:
                continue
            if selected and not feature.isSelected():
                continue
            yield feature

    def get_expressions(self, selected=False, ignore=()):
        """ """
        for expression in filter(
            lambda o: o.capa_type == CapaExplorerRulgenEditor.get_node_type_expression(), tuple(iterate_tree(self))
        ):
            if expression in ignore:
                continue
            if selected and not expression.isSelected():
                continue
            yield expression


class CapaExplorerRulegenFeatures(QtWidgets.QTreeWidget):
    def __init__(self, editor, parent=None):
        """ """
        super(CapaExplorerRulegenFeatures, self).__init__(parent)

        self.parent_items = {}
        self.editor = editor

        self.setHeaderLabels(["Feature", "Virtual Address"])
        self.header().setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        self.setStyleSheet("QTreeView::item {padding-right: 15 px;padding-bottom: 2 px;}")

        self.setExpandsOnDoubleClick(False)
        self.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)

        # connect slots
        self.itemDoubleClicked.connect(self.slot_item_double_clicked)
        self.customContextMenuRequested.connect(self.slot_custom_context_menu_requested)

        self.reset_view()

    @staticmethod
    def get_column_feature_index():
        """ """
        return 0

    @staticmethod
    def get_column_address_index():
        """ """
        return 1

    @staticmethod
    def get_node_type_parent():
        """ """
        return 0

    @staticmethod
    def get_node_type_leaf():
        """ """
        return 1

    def reset_view(self):
        """ """
        self.clear()

    def slot_add_selected_features(self, action):
        """ """
        selected = [item.data(0, 0x100) for item in self.selectedItems()]
        if selected:
            self.editor.update_features(selected)

    def slot_custom_context_menu_requested(self, pos):
        """ """
        actions = []
        action_add_features_fmt = ""

        selected_items_count = len(self.selectedItems())
        if selected_items_count == 0:
            return

        if selected_items_count == 1:
            action_add_features_fmt = "Add feature"
        else:
            action_add_features_fmt = "Add %d features" % selected_items_count

        actions.append((action_add_features_fmt, (), self.slot_add_selected_features))

        menu = build_context_menu(self.parent(), actions)
        menu.exec_(self.viewport().mapToGlobal(pos))

    def slot_item_double_clicked(self, o, column):
        """ """
        if column == CapaExplorerRulegenFeatures.get_column_address_index() and o.text(column):
            idc.jumpto(int(o.text(column), 0x10))
        elif o.capa_type == CapaExplorerRulegenFeatures.get_node_type_leaf():
            self.editor.update_features([o.data(0, 0x100)])

    def show_all_items(self):
        """ """
        for o in iterate_tree(self):
            o.setHidden(False)
            o.setExpanded(False)

    def filter_items_by_text(self, text):
        """ """
        if text:
            for o in iterate_tree(self):
                data = o.data(0, 0x100)
                if data and text.lower() not in data.get_value_str().lower():
                    o.setHidden(True)
                    continue
                o.setHidden(False)
                o.setExpanded(True)
        else:
            self.show_all_items()

    def style_parent_node(self, o):
        """ """
        font = QtGui.QFont()
        font.setBold(True)

        o.setFont(CapaExplorerRulegenFeatures.get_column_feature_index(), font)

    def style_leaf_node(self, o):
        """ """
        font = QtGui.QFont("Courier", weight=QtGui.QFont.Bold)
        brush = QtGui.QBrush()

        o.setFont(CapaExplorerRulegenFeatures.get_column_feature_index(), font)
        o.setFont(CapaExplorerRulegenFeatures.get_column_address_index(), font)

        brush.setColor(QtGui.QColor(*COLOR_GREEN_RGB))
        o.setForeground(CapaExplorerRulegenFeatures.get_column_feature_index(), brush)

        brush.setColor(QtGui.QColor(*COLOR_BLUE_RGB))
        o.setForeground(CapaExplorerRulegenFeatures.get_column_address_index(), brush)

    def set_parent_node(self, o):
        """ """
        o.setFlags(o.flags() & ~QtCore.Qt.ItemIsSelectable)
        setattr(o, "capa_type", CapaExplorerRulegenFeatures.get_node_type_parent())
        self.style_parent_node(o)

    def set_leaf_node(self, o):
        """ """
        setattr(o, "capa_type", CapaExplorerRulegenFeatures.get_node_type_leaf())
        self.style_leaf_node(o)

    def new_parent_node(self, parent, data, feature=None):
        """ """
        o = QtWidgets.QTreeWidgetItem(parent)

        self.set_parent_node(o)
        for (i, v) in enumerate(data):
            o.setText(i, v)
        if feature:
            o.setData(0, 0x100, feature)

        return o

    def new_leaf_node(self, parent, data, feature=None):
        """ """
        o = QtWidgets.QTreeWidgetItem(parent)

        self.set_leaf_node(o)
        for (i, v) in enumerate(data):
            o.setText(i, v)
        if feature:
            o.setData(0, 0x100, feature)

        return o

    def load_features(self, file_features, func_features={}):
        """ """
        self.parse_features_for_tree(self.new_parent_node(self, ("File Scope",)), file_features)
        if func_features:
            self.parse_features_for_tree(self.new_parent_node(self, ("Function/Basic Block Scope",)), func_features)

    def parse_features_for_tree(self, parent, features):
        """ """
        self.parent_items = {}

        def format_address(e):
            return "%X" % e if e else ""

        for (feature, eas) in sorted(features.items(), key=lambda k: sorted(k[1])):
            if isinstance(feature, capa.features.basicblock.BasicBlock):
                # filter basic blocks for now, we may want to add these back in some time
                # in the future
                continue

            if isinstance(feature, capa.features.String):
                # strip string for display
                feature.value = feature.value.strip()

            # level 0
            if type(feature) not in self.parent_items:
                self.parent_items[type(feature)] = self.new_parent_node(parent, (feature.name.lower(),))

            # level 1
            if feature not in self.parent_items:
                if len(eas) > 1:
                    self.parent_items[feature] = self.new_parent_node(
                        self.parent_items[type(feature)], (str(feature),), feature=feature
                    )
                else:
                    self.parent_items[feature] = self.new_leaf_node(
                        self.parent_items[type(feature)], (str(feature),), feature=feature
                    )

            # level n > 1
            if len(eas) > 1:
                for ea in sorted(eas):
                    self.new_leaf_node(self.parent_items[feature], (str(feature), format_address(ea)), feature=feature)
            else:
                ea = eas.pop()
                for (i, v) in enumerate((str(feature), format_address(ea))):
                    self.parent_items[feature].setText(i, v)
                self.parent_items[feature].setData(0, 0x100, feature)


class CapaExplorerQtreeView(QtWidgets.QTreeView):
    """tree view used to display hierarchical capa results

    view controls UI action responses and displays data from CapaExplorerDataModel

    view does not modify CapaExplorerDataModel directly - data modifications should be implemented
    in CapaExplorerDataModel
    """

    def __init__(self, model, parent=None):
        """initialize view"""
        super(CapaExplorerQtreeView, self).__init__(parent)

        self.setModel(model)

        self.model = model
        self.parent = parent

        # control when we resize columns
        self.should_resize_columns = True

        # configure custom UI controls
        self.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.setExpandsOnDoubleClick(False)
        self.setSortingEnabled(True)
        self.model.setDynamicSortFilter(False)

        # configure view columns to auto-resize
        for idx in range(CapaExplorerDataModel.COLUMN_COUNT):
            self.header().setSectionResizeMode(idx, QtWidgets.QHeaderView.Interactive)

        # disable stretch to enable horizontal scroll for last column, when needed
        self.header().setStretchLastSection(False)

        # connect slots to resize columns when expanded or collapsed
        self.expanded.connect(self.slot_resize_columns_to_content)
        self.collapsed.connect(self.slot_resize_columns_to_content)

        # connect slots
        self.customContextMenuRequested.connect(self.slot_custom_context_menu_requested)
        self.doubleClicked.connect(self.slot_double_click)

        self.setStyleSheet("QTreeView::item {padding-right: 15 px;padding-bottom: 2 px;}")

    def reset_ui(self, should_sort=True):
        """reset user interface changes

        called when view should reset UI display e.g. expand items, resize columns

        @param should_sort: True, sort results after reset, False don't sort results after reset
        """
        if should_sort:
            self.sortByColumn(CapaExplorerDataModel.COLUMN_INDEX_RULE_INFORMATION, QtCore.Qt.AscendingOrder)

        self.should_resize_columns = False
        self.expandToDepth(0)
        self.should_resize_columns = True

        self.slot_resize_columns_to_content()

    def slot_resize_columns_to_content(self):
        """reset view columns to contents"""
        if self.should_resize_columns:
            self.header().resizeSections(QtWidgets.QHeaderView.ResizeToContents)

            # limit size of first section
            if self.header().sectionSize(0) > MAX_SECTION_SIZE:
                self.header().resizeSection(0, MAX_SECTION_SIZE)

    def map_index_to_source_item(self, model_index):
        """map proxy model index to source model item

        @param model_index: QModelIndex

        @retval QObject
        """
        # assume that self.model here is either:
        #  - CapaExplorerDataModel, or
        #  - QSortFilterProxyModel subclass
        #
        # The ProxyModels may be chained,
        #  so keep resolving the index the CapaExplorerDataModel.

        model = self.model
        while not isinstance(model, CapaExplorerDataModel):
            if not model_index.isValid():
                raise ValueError("invalid index")

            model_index = model.mapToSource(model_index)
            model = model.sourceModel()

        if not model_index.isValid():
            raise ValueError("invalid index")

        return model_index.internalPointer()

    def send_data_to_clipboard(self, data):
        """copy data to the clipboard

        @param data: data to be copied
        """
        clip = QtWidgets.QApplication.clipboard()
        clip.clear(mode=clip.Clipboard)
        clip.setText(data, mode=clip.Clipboard)

    def new_action(self, display, data, slot):
        """create action for context menu

        @param display: text displayed to user in context menu
        @param data: data passed to slot
        @param slot: slot to connect

        @retval QAction
        """
        action = QtWidgets.QAction(display, self.parent)
        action.setData(data)
        action.triggered.connect(lambda checked: slot(action))

        return action

    def load_default_context_menu_actions(self, data):
        """yield actions specific to function custom context menu

        @param data: tuple

        @yield QAction
        """
        default_actions = (
            ("Copy column", data, self.slot_copy_column),
            ("Copy row", data, self.slot_copy_row),
        )

        # add default actions
        for action in default_actions:
            yield self.new_action(*action)

    def load_function_context_menu_actions(self, data):
        """yield actions specific to function custom context menu

        @param data: tuple

        @yield QAction
        """
        function_actions = (("Rename function", data, self.slot_rename_function),)

        # add function actions
        for action in function_actions:
            yield self.new_action(*action)

        # add default actions
        for action in self.load_default_context_menu_actions(data):
            yield action

    def load_default_context_menu(self, pos, item, model_index):
        """create default custom context menu

        creates custom context menu containing default actions

        @param pos: cursor position
        @param item: CapaExplorerDataItem
        @param model_index: QModelIndex

        @retval QMenu
        """
        menu = QtWidgets.QMenu()

        for action in self.load_default_context_menu_actions((pos, item, model_index)):
            menu.addAction(action)

        return menu

    def load_function_item_context_menu(self, pos, item, model_index):
        """create function custom context menu

        creates custom context menu with both default actions and function actions

        @param pos: cursor position
        @param item: CapaExplorerDataItem
        @param model_index: QModelIndex

        @retval QMenu
        """
        menu = QtWidgets.QMenu()

        for action in self.load_function_context_menu_actions((pos, item, model_index)):
            menu.addAction(action)

        return menu

    def show_custom_context_menu(self, menu, pos):
        """display custom context menu in view

        @param menu: QMenu to display
        @param pos: cursor position
        """
        if menu:
            menu.exec_(self.viewport().mapToGlobal(pos))

    def slot_copy_column(self, action):
        """slot connected to custom context menu

        allows user to select a column and copy the data to clipboard

        @param action: QAction
        """
        _, item, model_index = action.data()
        self.send_data_to_clipboard(item.data(model_index.column()))

    def slot_copy_row(self, action):
        """slot connected to custom context menu

        allows user to select a row and copy the space-delimited data to clipboard

        @param action: QAction
        """
        _, item, _ = action.data()
        self.send_data_to_clipboard(str(item))

    def slot_rename_function(self, action):
        """slot connected to custom context menu

        allows user to select a edit a function name and push changes to IDA

        @param action: QAction
        """
        _, item, model_index = action.data()

        # make item temporary edit, reset after user is finished
        item.setIsEditable(True)
        self.edit(model_index)
        item.setIsEditable(False)

    def slot_custom_context_menu_requested(self, pos):
        """slot connected to custom context menu request

        displays custom context menu to user containing action relevant to the item selected

        @param pos: cursor position
        """
        model_index = self.indexAt(pos)

        if not model_index.isValid():
            return

        item = self.map_index_to_source_item(model_index)

        column = model_index.column()
        menu = None

        if CapaExplorerDataModel.COLUMN_INDEX_RULE_INFORMATION == column and isinstance(item, CapaExplorerFunctionItem):
            # user hovered function item
            menu = self.load_function_item_context_menu(pos, item, model_index)
        else:
            # user hovered default item
            menu = self.load_default_context_menu(pos, item, model_index)

        # show custom context menu at view position
        self.show_custom_context_menu(menu, pos)

    def slot_double_click(self, model_index):
        """slot connected to double-click event

        if address column clicked, navigate IDA to address, else un/expand item clicked

        @param model_index: QModelIndex
        """
        if not model_index.isValid():
            return

        item = self.map_index_to_source_item(model_index)
        column = model_index.column()

        if CapaExplorerDataModel.COLUMN_INDEX_VIRTUAL_ADDRESS == column and item.location:
            # user double-clicked virtual address column - navigate IDA to address
            idc.jumpto(item.location)

        if CapaExplorerDataModel.COLUMN_INDEX_RULE_INFORMATION == column:
            # user double-clicked information column - un/expand
            self.collapse(model_index) if self.isExpanded(model_index) else self.expand(model_index)
