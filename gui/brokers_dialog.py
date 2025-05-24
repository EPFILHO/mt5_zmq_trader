# gui/brokers_dialog.py
import logging
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QLineEdit,
    QPushButton, QMessageBox, QToolButton
)
from PySide6.QtCore import Signal, Qt
from PySide6.QtGui import QIcon, QPixmap, QStandardItemModel, QStandardItem, QColor

logger = logging.getLogger(__name__)

EYE_OPEN_SVG = '''
<svg width="24" height="24" viewBox="0 0 24 24">
<path fill="green" d="M12 4.5C7 4.5 2.73 7.61 1 12c1.73 4.39 6 7.5 11 7.5s9.27-3.11 11-7.5C21.27 7.61 17 4.5 12 4.5zm0 13c-3.87 0-7.19-2.54-8.48-6C4.81 8.04 8.13 5.5 12 5.5s7.19 2.54 8.48 6c-1.29 3.46-4.61 6-8.48 6zm0-10a4 4 0 100 8 4 4 0 000-8zm0 6.5a2.5 2.5 0 110-5 2.5 2.5 0 010 5z"/>
</svg>
'''
EYE_CLOSED_SVG = '''
<svg width="24" height="24" viewBox="0 0 24 24">
<path fill="red" d="M12 4.5C7 4.5 2.73 7.61 1 12c1.73 4.39 6 7.5 11 7.5s9.27-3.11 11-7.5C21.27 7.61 17 4.5 12 4.5zm0 13c-3.87 0-7.19-2.54-8.48-6C4.81 8.04 8.13 5.5 12 5.5s7.19 2.54 8.48 6c-1.29 3.46-4.61 6-8.48 6zm0-10a4 4 0 100 8 4 4 0 000-8zm0 6.5a2.5 2.5 0 110-5 2.5 2.5 0 010 5z"/>
</svg>
'''

def svg_icon(svg_data):
    pixmap = QPixmap()
    pixmap.loadFromData(bytearray(svg_data, encoding='utf-8'), "SVG")
    return QIcon(pixmap)

class BrokersDialog(QDialog):
    brokers_updated = Signal()

    def __init__(self, config, broker_manager, parent=None):
        super().__init__(parent)
        self.config = config
        self.broker_manager = broker_manager
        self.setWindowTitle("Cadastro de Corretoras")
        self.setMinimumWidth(400)
        self._init_ui()
        self._populate_brokers()
        self._clear_fields()
        self._connect_signals()
        self.setModal(True)

    def showEvent(self, event):
        super().showEvent(event)
        self._populate_brokers()
        self._clear_fields()
        self.combo.setCurrentIndex(-1)

    def _init_ui(self):
        layout = QVBoxLayout(self)

        select_layout = QHBoxLayout()
        select_label = QLabel("Selecionar corretora:")
        self.combo = QComboBox()
        select_layout.addWidget(select_label)
        select_layout.addWidget(self.combo)
        layout.addLayout(select_layout)

        self.name_edit = QLineEdit()
        self.client_edit = QLineEdit()
        self.broker_name_edit = QLineEdit()
        self.login_edit = QLineEdit()

        password_layout = QHBoxLayout()
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.Password)
        password_layout.addWidget(self.password_edit)
        self.show_password_btn = QToolButton()
        self.show_password_btn.setCheckable(True)
        self.show_password_btn.setIcon(svg_icon(EYE_OPEN_SVG))
        self.show_password_btn.setToolTip("Exibir/ocultar senha")
        self.show_password_btn.setStyleSheet("""
            QToolButton {
                background: transparent;
                border: none;
                outline: none;
                padding: 0px;
                margin: 0px;
            }
            QToolButton:hover {
                background: transparent;
            }
            QToolButton:focus {
                background: transparent;
                outline: none;
            }
            QToolButton:checked {
                background: transparent;
            }
            QToolButton:pressed {
                background: transparent;
            }
        """)
        password_layout.addWidget(self.show_password_btn)

        self.server_edit = QLineEdit()

        layout.addWidget(QLabel("Nome do Titular:"))
        layout.addWidget(self.name_edit)
        layout.addWidget(QLabel("Cliente:"))
        layout.addWidget(self.client_edit)
        layout.addWidget(QLabel("Nome da Corretora:"))
        layout.addWidget(self.broker_name_edit)
        layout.addWidget(QLabel("Login:"))
        layout.addWidget(self.login_edit)
        layout.addWidget(QLabel("Senha:"))
        layout.addLayout(password_layout)
        layout.addWidget(QLabel("Servidor:"))
        layout.addWidget(self.server_edit)

        mode_type_layout = QHBoxLayout()
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["Hedge", "Netting"])
        self.type_combo = QComboBox()
        self.type_combo.addItems(["Demo", "Real"])
        mode_type_layout.addWidget(QLabel("Modo:"))
        mode_type_layout.addWidget(self.mode_combo)
        mode_type_layout.addSpacing(20)
        mode_type_layout.addWidget(QLabel("Tipo:"))
        mode_type_layout.addWidget(self.type_combo)
        layout.addLayout(mode_type_layout)

        btn_layout = QHBoxLayout()
        self.add_or_clear_btn = QPushButton("Adicionar")
        self.modify_btn = QPushButton("Modificar")
        self.remove_btn = QPushButton("Excluir")
        self.close_btn = QPushButton("Fechar")
        btn_layout.addWidget(self.add_or_clear_btn)
        btn_layout.addWidget(self.modify_btn)
        btn_layout.addWidget(self.remove_btn)
        btn_layout.addWidget(self.close_btn)
        layout.addLayout(btn_layout)

        info_label = QLabel("Não é possível modificar ou excluir uma corretora conectada.")
        info_label.setStyleSheet("color: red; font-style: italic;")
        layout.addWidget(info_label)

    def _connect_signals(self):
        self.combo.currentIndexChanged.connect(self._on_combo_changed)
        self.add_or_clear_btn.clicked.connect(self._on_add_or_clear_clicked)
        self.modify_btn.clicked.connect(self._on_modify_clicked)
        self.remove_btn.clicked.connect(self._on_remove_clicked)
        self.close_btn.clicked.connect(self.close)
        self.show_password_btn.toggled.connect(self._toggle_password_visibility)
        self.name_edit.textChanged.connect(self._update_buttons)
        self.client_edit.textChanged.connect(self._update_buttons)
        self.broker_name_edit.textChanged.connect(self._update_buttons)
        self.login_edit.textChanged.connect(self._update_buttons)
        self.password_edit.textChanged.connect(self._update_buttons)
        self.server_edit.textChanged.connect(self._update_buttons)
        self.mode_combo.currentIndexChanged.connect(self._update_buttons)
        self.type_combo.currentIndexChanged.connect(self._update_buttons)

    def _populate_brokers(self):
        self.combo.blockSignals(True)
        self.combo.clear()
        self._broker_keys = []
        brokers = self.broker_manager.get_brokers()
        connected = self.broker_manager.get_connected_brokers()

        model = QStandardItemModel()
        for key in sorted(brokers.keys()):
            item = QStandardItem(key)
            is_connected = key in connected
            item.setForeground(QColor("red" if is_connected else "green"))
            item.setData(is_connected, Qt.UserRole)
            item.setData(QColor("red" if is_connected else "green"), Qt.ForegroundRole)
            model.appendRow(item)
            self._broker_keys.append(key)
        self.combo.setModel(model)
        self.combo.setCurrentIndex(-1)
        self.combo.blockSignals(False)
        self._clear_fields()
        self._update_buttons()
        logger.debug(f"Populating brokers: {list(brokers.keys())}, connected: {list(connected)}")

    def _on_combo_changed(self, idx):
        if idx < 0 or idx >= len(self._broker_keys):
            self._clear_fields()
            self._update_buttons()
            self.combo.setStyleSheet("QComboBox { color: black; }")
            logger.debug("ComboBox resetado para cor preta (sem seleção)")
            return
        key = self._broker_keys[idx]
        broker = self.broker_manager.get_brokers().get(key, {})
        self.name_edit.setText(broker.get("name", ""))
        self.client_edit.setText(broker.get("client", ""))
        self.broker_name_edit.setText(broker.get("broker_name", key.split("-")[0]))
        self.login_edit.setText(broker.get("login", ""))
        self.password_edit.setText(broker.get("password", ""))
        self.server_edit.setText(broker.get("server", ""))
        mode = broker.get("mode", "Hedge")
        type_ = broker.get("type", "Demo")
        self.mode_combo.setCurrentIndex(self.mode_combo.findText(mode) if mode in ["Hedge", "Netting"] else 0)
        self.type_combo.setCurrentIndex(self.type_combo.findText(type_) if type_ in ["Demo", "Real"] else 0)
        is_connected = key in self.broker_manager.get_connected_brokers()
        self.combo.setStyleSheet(f"QComboBox {{ color: {'red' if is_connected else 'green'}; }}")
        logger.debug(f"ComboBox configurado para cor {'red' if is_connected else 'green'} (key={key}, is_connected={is_connected})")
        self._update_buttons()

    def _clear_fields(self):
        self.name_edit.clear()
        self.client_edit.clear()
        self.broker_name_edit.clear()
        self.login_edit.clear()
        self.password_edit.clear()
        self.server_edit.clear()
        self.mode_combo.setCurrentIndex(0)
        self.type_combo.setCurrentIndex(0)
        self.combo.setCurrentIndex(-1)
        self._update_buttons()

    def _update_buttons(self):
        idx = self.combo.currentIndex()
        has_selection = idx >= 0 and idx < len(self._broker_keys)
        key = self._broker_keys[idx] if has_selection else None
        is_connected = key in self.broker_manager.get_connected_brokers() if key else False

        all_fields_filled = all([
            self.name_edit.text().strip(),
            self.client_edit.text().strip(),
            self.broker_name_edit.text().strip(),
            self.login_edit.text().strip(),
            self.password_edit.text().strip(),
            self.server_edit.text().strip(),
            self.mode_combo.currentText().strip(),
            self.type_combo.currentText().strip()
        ])
        if has_selection:
            self.add_or_clear_btn.setText("Limpar")
            self.add_or_clear_btn.setEnabled(True)
        else:
            self.add_or_clear_btn.setText("Adicionar")
            self.add_or_clear_btn.setEnabled(all_fields_filled)
        self.modify_btn.setEnabled(has_selection and not is_connected)
        self.remove_btn.setEnabled(has_selection and not is_connected)
        return all_fields_filled

    def _generate_ports(self):
        """
        Gera um bloco de portas ZMQ não utilizadas para uma nova corretora.
        Retorna: admin_port, data_port, live_port, str_port, trade_port (todos int)
        """
        BASE_PORT = 15555
        MAX_PORT = 65530
        STEP = 5

        used_ports = set()
        for broker in self.broker_manager.get_brokers().values():
            for port_name in ["admin_port", "data_port", "live_port", "str_port", "trade_port"]:
                port = broker.get(port_name)
                if port:
                    used_ports.add(int(port))

        for start in range(BASE_PORT, MAX_PORT, STEP):
            block = [start + i for i in range(STEP)]
            if not any(port in used_ports for port in block):
                return tuple(block)
        raise RuntimeError("Não há blocos de portas ZMQ disponíveis.")

    def _on_add_or_clear_clicked(self):
        idx = self.combo.currentIndex()
        has_selection = idx >= 0 and idx < len(self._broker_keys)
        if has_selection:
            self._clear_fields()
            return

        if not self._update_buttons():
            QMessageBox.warning(self, "Campos obrigatórios", "Preencha todos os campos para adicionar.")
            return

        data = {
            "name": self.name_edit.text().strip(),
            "client": self.client_edit.text().strip(),
            "broker_name": self.broker_name_edit.text().strip(),
            "login": self.login_edit.text().strip(),
            "password": self.password_edit.text().strip(),
            "server": self.server_edit.text().strip(),
            "mode": self.mode_combo.currentText().strip(),
            "type_": self.type_combo.currentText().strip()
        }

        try:
            admin_port, data_port, live_port, str_port, trade_port = self._generate_ports()
            data.update({
                "admin_port": admin_port,
                "data_port": data_port,
                "live_port": live_port,
                "str_port": str_port,
                "trade_port": trade_port
            })
        except Exception as e:
            QMessageBox.critical(self, "Erro de portas", f"Erro ao gerar portas ZMQ: {e}")
            return

        key = self.broker_manager.add_broker(**data)
        if key:
            QMessageBox.information(self, "Sucesso", f"Corretora '{key}' adicionada com sucesso.")
            self._populate_brokers()
            self.brokers_updated.emit()
            if hasattr(self.parent(), "main_menu"):
                self.parent().main_menu._populate_conn_menu()
        else:
            QMessageBox.warning(self, "Erro", "Não foi possível adicionar a corretora.")

    def _on_modify_clicked(self):
        idx = self.combo.currentIndex()
        if idx < 0 or idx >= len(self._broker_keys):
            return
        old_key = self._broker_keys[idx]

        if not self._update_buttons():
            QMessageBox.warning(self, "Campos obrigatórios", "Preencha todos os campos para modificar.")
            return

        data = {
            "name": self.name_edit.text().strip(),
            "client": self.client_edit.text().strip(),
            "broker_name": self.broker_name_edit.text().strip(),
            "login": self.login_edit.text().strip(),
            "password": self.password_edit.text().strip(),
            "server": self.server_edit.text().strip(),
            "mode": self.mode_combo.currentText().strip(),
            "type_": self.type_combo.currentText().strip()
        }

        # Mantém as portas já cadastradas para a corretora
        broker = self.broker_manager.get_brokers().get(old_key, {})
        data.update({
            "admin_port": broker.get("admin_port"),
            "data_port": broker.get("data_port"),
            "live_port": broker.get("live_port"),
            "str_port": broker.get("str_port"),
            "trade_port": broker.get("trade_port")
        })

        new_key = self.broker_manager.modify_broker(old_key, **data)
        if new_key:
            QMessageBox.information(self, "Sucesso", f"Corretora '{old_key}' modificada para '{new_key}'.")
            self._populate_brokers()
            self.brokers_updated.emit()
            if hasattr(self.parent(), "main_menu"):
                self.parent().main_menu._populate_conn_menu()
        else:
            QMessageBox.warning(self, "Erro", "Não foi possível modificar a corretora.")

    def _on_remove_clicked(self):
        idx = self.combo.currentIndex()
        if idx < 0 or idx >= len(self._broker_keys):
            return
        key = self._broker_keys[idx]
        reply = QMessageBox.question(
            self, "Confirmação", f"Tem certeza que deseja excluir a corretora '{key}'?",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            if self.broker_manager.remove_broker(key):
                QMessageBox.information(self, "Sucesso", f"Corretora '{key}' excluída com sucesso.")
                self._populate_brokers()
                self.brokers_updated.emit()
                if hasattr(self.parent(), "main_menu"):
                    self.parent().main_menu._populate_conn_menu()
            else:
                QMessageBox.warning(self, "Erro", "Não foi possível excluir a corretora.")

    def _toggle_password_visibility(self, checked):
        if checked:
            self.password_edit.setEchoMode(QLineEdit.Normal)
            self.show_password_btn.setIcon(svg_icon(EYE_CLOSED_SVG))
        else:
            self.password_edit.setEchoMode(QLineEdit.Password)
            self.show_password_btn.setIcon(svg_icon(EYE_OPEN_SVG))

#versão 1.0.9.a - envio 2