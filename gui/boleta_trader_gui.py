import sys
import json
import time
import asyncio
from PySide6.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout, QTabWidget, QTableWidget,
    QTableWidgetItem, QPushButton, QTextEdit, QLabel, QDoubleSpinBox,
    QAbstractItemView,
)
from PySide6.QtCore import Slot, Qt
import logging

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class BoletaTraderGui(QDialog):
    def __init__(self, config, broker_manager, zmq_router, zmq_message_handler, main_window, parent=None):
        super().__init__(parent)
        self.config = config
        self.broker_manager = broker_manager
        self.zmq_router = zmq_router
        self.zmq_message_handler = zmq_message_handler
        self.main_window = main_window
        self.broker_status = {}
        self.broker_connected = {}
        self.broker_modes = {}
        self.positions_requested = {}
        self.pending_tickets = {}
        self.setWindowTitle("Boleta Trader GUI")
        self.setGeometry(100, 100, 1000, 600)
        self.setMinimumWidth(1000)
        self._update_broker_status_initial()
        self.setup_ui()
        self._connect_signals()
        self._populate_broker_tabs()
        self._request_positions_for_registered_brokers()
        logger.info("BoletaTraderGui inicializado.")

    def setup_ui(self):
        layout = QVBoxLayout(self)
        self.broker_tabs = QTabWidget()
        self.broker_tabs.setStyleSheet("""
            QTabBar::tab:selected {
                font-weight: bold;
            }
        """)  # Negrito na aba selecionada
        layout.addWidget(self.broker_tabs)
        self.log_area = QTextEdit()
        self.log_area.setReadOnly(True)
        self.log_area.setMinimumHeight(100)
        layout.addWidget(QLabel("Log de Atividades:"))
        layout.addWidget(self.log_area)
        control_layout = QHBoxLayout()
        update_btn = QPushButton("Atualizar Agora")
        update_btn.clicked.connect(self._request_positions)
        update_btn.setStyleSheet("""
            QPushButton {
                background-color: #4CAF50;
                color: white;
                padding: 5px;
                border-radius: 3px;
            }
            QPushButton:hover {
                background-color: #45A049;
            }
        """)
        close_btn = QPushButton("Fechar Janela")
        close_btn.clicked.connect(self.close)
        close_btn.setStyleSheet("""
            QPushButton {
                background-color: #ff3333;
                color: white;
                padding: 5px;
                border-radius: 3px;
            }
            QPushButton:hover {
                background-color: #e62e2e;
            }
        """)
        control_layout.addWidget(update_btn)
        control_layout.addWidget(close_btn)
        control_layout.setAlignment(Qt.AlignCenter)
        layout.addLayout(control_layout)

    def _update_broker_status_initial(self):
        try:
            if hasattr(self.main_window, 'broker_status') and hasattr(self.main_window, 'broker_modes'):
                self.broker_status.update(self.main_window.broker_status)
                self.broker_modes.update(self.main_window.broker_modes)
                logger.debug(f"Status de registro atualizado: {self.broker_status}")
                logger.debug(f"Modos de corretoras atualizados: {self.broker_modes}")
            else:
                logger.warning("main_window não possui broker_status ou broker_modes ao iniciar.")
            connected_brokers = self.broker_manager.get_connected_brokers()
            for broker_key in self.broker_manager.get_brokers():
                self.broker_connected[broker_key] = broker_key in connected_brokers
            logger.debug(f"Status de conexão atualizado no início: {self.broker_connected}")
        except Exception as e:
            logger.error(f"Erro ao atualizar status inicial de corretoras: {str(e)}")
            self.update_log(f"Erro ao atualizar status inicial de corretoras: {str(e)}")

    def _connect_signals(self):
        self.zmq_message_handler.log_message_received.connect(self.update_log)
        self.zmq_message_handler.positions_received.connect(self._update_positions)
        self.zmq_message_handler.trade_response_received.connect(self._update_trade_response)
        self.main_window.broker_status_updated.connect(self._on_broker_status_updated)
        self.main_window.broker_connected.connect(self._on_broker_connected)
        logger.debug("Sinais conectados no BoletaTraderGui.")

    def _populate_broker_tabs(self):
        all_brokers = self.broker_manager.get_brokers()
        existing_tabs = {self.broker_tabs.tabText(i).split(" (")[0]: i for i in range(self.broker_tabs.count())}

        logger.debug(f"Populando abas com modos: {self.broker_modes}")

        # Remover abas de corretoras não conectadas/registradas
        for tab_key in list(existing_tabs.keys()):
            if (tab_key not in all_brokers or
                not self.broker_connected.get(tab_key, False) or
                not self.broker_status.get(tab_key, False)):
                self.broker_tabs.removeTab(existing_tabs[tab_key])
                logger.info(f"Aba removida para corretora {tab_key} (não conectada/registrada).")
                del existing_tabs[tab_key]

        # Criar abas apenas para corretoras conectadas e registradas
        for key in sorted(all_brokers.keys()):
            if (key not in existing_tabs and
                self.broker_connected.get(key, False) and
                self.broker_status.get(key, False)):
                mode = self.broker_modes.get(key, "Hedge")
                tab_label = f"{key} ({'H' if mode == 'Hedge' else 'N'})"
                logger.debug(f"Criando aba para {key} com modo {mode}: {tab_label}")
                tab = QWidget()
                tab_layout = QVBoxLayout(tab)
                table = QTableWidget()
                table.setColumnCount(11)
                table.setHorizontalHeaderLabels(
                    ["Ticket", "Símbolo", "Tipo", "Volume", "Preço Entrada", "SL", "TP", "Lucro/Prejuízo", "Fechar",
                     "Modificar", "Parcial"])
                table.setColumnWidth(0, 80)
                table.setColumnWidth(1, 100)
                table.setColumnWidth(2, 80)
                table.setColumnWidth(3, 80)
                table.setColumnWidth(4, 100)
                table.setColumnWidth(5, 80)
                table.setColumnWidth(6, 80)
                table.setColumnWidth(7, 120)
                table.setColumnWidth(8, 70)
                table.setColumnWidth(9, 70)
                table.setColumnWidth(10, 70)
                table.setMinimumHeight(400)
                table.setRowHeight(0, 30)
                table.setEditTriggers(QAbstractItemView.NoEditTriggers)  # Impedir edição
                table.setSelectionMode(QAbstractItemView.NoSelection)  # Impedir seleção
                table.setAlternatingRowColors(True)  # Efeito listrado
                table.setStyleSheet("""
                    QTableWidget {
                        alternate-background-color: #f0f0f0;
                    }
                """)  # Cor alternada para linhas
                tab_layout.addWidget(table)
                self.broker_tabs.addTab(tab, tab_label)
                logger.info(f"Aba criada para corretora {key} com modo {mode}.")

        # Definir índice -1 se não houver abas ativas
        if self.broker_tabs.count() == 0:
            self.broker_tabs.setCurrentIndex(-1)
        logger.info(f"Abas de corretoras atualizadas: {self.broker_tabs.count()} corretoras listadas.")

    def _request_positions_for_registered_brokers(self):
        if hasattr(self.main_window, 'broker_status'):
            self.broker_status.update(self.main_window.broker_status)
            connected_brokers = self.broker_manager.get_connected_brokers()
            for broker_key in connected_brokers:
                if broker_key in self.broker_status and self.broker_status[broker_key]:
                    self._request_positions_for_broker(broker_key)
                    self.positions_requested[broker_key] = True
                    logger.info(f"Posições solicitadas para corretora registrada {broker_key} ao iniciar BoletaTraderGui.")
                    self.update_log(f"Solicitando posições para {broker_key}...")
                else:
                    logger.info(f"Aguardando registro de {broker_key} ao iniciar BoletaTraderGui.")
        else:
            logger.warning("main_window não possui broker_status ao iniciar BoletaTraderGui.")

    @Slot(dict, dict)
    def _on_broker_status_updated(self, broker_status, broker_modes):
        try:
            self.broker_status.update(broker_status)
            self.broker_modes.update(broker_modes)
            logger.debug(f"Status de corretoras atualizado: {self.broker_status}")
            logger.debug(f"Modos de corretoras atualizado: {self.broker_modes}")
            self._populate_broker_tabs()  # Atualizar abas para refletir status
            for broker_key in self.broker_status:
                if broker_key in self.broker_connected and self.broker_connected[broker_key] and \
                   self.broker_status[broker_key] and not self.positions_requested.get(broker_key, False):
                    self._request_positions_for_broker(broker_key)
                    self.positions_requested[broker_key] = True
                    self.update_log(f"Solicitando posições para {broker_key}...")
            self.broker_tabs.setCurrentIndex(-1)  # Nenhuma aba selecionada por padrão
            logger.info(f"Status de corretoras atualizado: {self.broker_status}")
        except Exception as e:
            logger.error(f"Erro ao atualizar status de corretoras: {str(e)}")
            self.update_log(f"Erro ao atualizar status de corretoras: {str(e)}")

    @Slot()
    def _on_broker_connected(self, *args, **kwargs):
        connected_brokers = self.broker_manager.get_connected_brokers()
        for broker_key in self.broker_manager.get_brokers():
            self.broker_connected[broker_key] = broker_key in connected_brokers
        self._populate_broker_tabs()  # Atualizar abas para refletir conexões
        self.broker_tabs.setCurrentIndex(-1)  # Nenhuma aba selecionada por padrão
        logger.info(f"Status de conexão atualizado: {self.broker_connected}")

    def _request_positions(self):
        for broker_key in self.broker_manager.get_connected_brokers():
            self.positions_requested[broker_key] = False
        connected_brokers = self.broker_manager.get_connected_brokers()
        for broker_key in connected_brokers:
            if broker_key in self.broker_status and self.broker_status[broker_key]:
                self._request_positions_for_broker(broker_key)
                self.positions_requested[broker_key] = True
                self.update_log(f"Solicitando posições para {broker_key}...")
            else:
                self.update_log(f"Aguardando registro de {broker_key} antes de solicitar posições.")
                logger.info(f"Aguardando registro de {broker_key} antes de solicitar posições.")

    def _request_positions_for_broker(self, broker_key):
        command = "POSITIONS"
        payload = {}
        request_id = f"positions_{broker_key}_{int(time.time())}"
        self._send_async_command(broker_key, command, payload, request_id)
        logger.info(f"Comando agendado: Solicitar posições ({command}) para {broker_key} com request_id {request_id}")

    def _send_async_command(self, broker_key, command, payload, request_id):
        try:
            asyncio.create_task(
                self.zmq_router.send_command_to_broker(broker_key, command, payload, request_id)
            )
            logger.info(f"Comando {command} enviado para {broker_key} com request_id: {request_id}")
        except Exception as e:
            logger.error(f"Erro ao enviar comando {command} para {broker_key}: {str(e)}")
            self.update_log(f"Erro ao enviar comando para {broker_key}: {str(e)}")

    @Slot(dict)
    def _update_positions(self, positions_data):
        try:
            broker_key = positions_data.get("broker_key", "")
            positions = positions_data.get("data", positions_data.get("", []))
            logger.info(f"Atualizando posições na GUI para {broker_key}, {len(positions)} ordens recebidas.")
            for i in range(self.broker_tabs.count()):
                if self.broker_tabs.tabText(i).startswith(broker_key):
                    tab = self.broker_tabs.widget(i)
                    table = tab.layout().itemAt(0).widget()
                    table.clearContents()
                    table.setRowCount(len(positions))
                    for row, pos in enumerate(positions):
                        ticket_item = QTableWidgetItem(str(pos.get("ticket", "")))
                        ticket_item.setTextAlignment(Qt.AlignCenter)
                        table.setItem(row, 0, ticket_item)

                        symbol_item = QTableWidgetItem(str(pos.get("symbol", "")))
                        symbol_item.setTextAlignment(Qt.AlignCenter)
                        table.setItem(row, 1, symbol_item)

                        type_item = QTableWidgetItem(str(pos.get("type", "")))
                        type_item.setTextAlignment(Qt.AlignCenter)
                        table.setItem(row, 2, type_item)

                        volume_item = QTableWidgetItem(str(pos.get("volume", 0.0)))
                        volume_item.setTextAlignment(Qt.AlignCenter)
                        table.setItem(row, 3, volume_item)

                        price_open_item = QTableWidgetItem(str(pos.get("price_open", 0.0)))
                        price_open_item.setTextAlignment(Qt.AlignCenter)
                        table.setItem(row, 4, price_open_item)

                        sl_item = QTableWidgetItem(str(pos.get("sl", 0.0)))
                        sl_item.setTextAlignment(Qt.AlignCenter)
                        table.setItem(row, 5, sl_item)

                        tp_item = QTableWidgetItem(str(pos.get("tp", 0.0)))
                        tp_item.setTextAlignment(Qt.AlignCenter)
                        table.setItem(row, 6, tp_item)

                        profit_item = QTableWidgetItem(str(pos.get("profit", 0.0)))
                        profit_item.setTextAlignment(Qt.AlignCenter)
                        table.setItem(row, 7, profit_item)

                        close_btn = QPushButton("✕")
                        close_btn.setMinimumHeight(30)
                        close_btn.setStyleSheet("color: red; padding: 0px; margin: 0px;")
                        close_btn.clicked.connect(lambda checked, r=row, bk=broker_key: self._close_order(r, bk))

                        modify_btn = QPushButton("⚠")
                        modify_btn.setMinimumHeight(30)
                        modify_btn.setStyleSheet("padding: 0px; margin: 0px;")
                        modify_btn.clicked.connect(lambda checked, r=row, bk=broker_key: self._modify_order(r, bk))

                        partial_btn = QPushButton("½")
                        partial_btn.setMinimumHeight(30)
                        partial_btn.setStyleSheet("padding: 0px; margin: 0px;")
                        partial_btn.clicked.connect(lambda checked, r=row, bk=broker_key: self._partial_close(r, bk))

                        enabled = broker_key in self.broker_status and self.broker_status[broker_key]
                        close_btn.setEnabled(enabled)
                        modify_btn.setEnabled(enabled)
                        partial_btn.setEnabled(enabled)

                        table.setCellWidget(row, 8, close_btn)
                        table.setCellWidget(row, 9, modify_btn)
                        table.setCellWidget(row, 10, partial_btn)
                        logger.debug(f"Botões de ação adicionados para ordem na linha {row} de {broker_key}.")
                    break
            else:
                logger.warning(f"Aba não encontrada para corretora {broker_key}.")
                self.update_log(f"Erro: Aba não encontrada para corretora {broker_key}.")
            self.update_log(f"Lista de posições atualizada para {broker_key} com {len(positions)} ordens.")
        except Exception as e:
            logger.error(f"Erro ao atualizar posições na GUI: {str(e)}")
            self.update_log(f"Erro ao atualizar posições na GUI: {str(e)}")

    def _close_order(self, row, broker_key):
        tab = self.broker_tabs.widget(self.broker_tabs.currentIndex())
        if tab and self.broker_tabs.tabText(self.broker_tabs.currentIndex()).startswith(broker_key):
            table = tab.layout().itemAt(0).widget()
            ticket = table.item(row, 0).text() if table.item(row, 0) else ""
            if ticket:
                command = "TRADE_POSITION_CLOSE_ID"
                payload = {"ticket": int(ticket)}
                request_id = f"close_{broker_key}_{int(time.time())}"
                self.pending_tickets[request_id] = ticket
                self._send_async_command(broker_key, command, payload, request_id)
                self.update_log(
                    f"Comando enviado: Fechar ordem #{ticket} para {broker_key} às {time.strftime('%H:%M:%S', time.localtime())}.")
                logger.info(f"Comando agendado: Fechar ordem #{ticket} para {broker_key} com request_id {request_id}")
            else:
                self.update_log("Erro: Ticket da ordem não encontrado.")

    def _modify_order(self, row, broker_key):
        tab = self.broker_tabs.widget(self.broker_tabs.currentIndex())
        if tab and self.broker_tabs.tabText(self.broker_tabs.currentIndex()).startswith(broker_key):
            table = tab.layout().itemAt(0).widget()
            ticket = table.item(row, 0).text() if table.item(row, 0) else ""
            symbol = table.item(row, 1).text() if table.item(row, 1) else ""
            order_type = table.item(row, 2).text() if table.item(row, 2) else ""
            if not ticket or not symbol:
                self.update_log("Erro: Ticket ou símbolo da ordem não encontrado.")
                return
            modify_dialog = QDialog(self)
            modify_dialog.setWindowTitle(f"Modificar Ordem #{ticket}")
            layout = QVBoxLayout(modify_dialog)
            sl_input = QDoubleSpinBox()
            sl_input.setDecimals(2)
            sl_input.setMinimum(0.0)
            sl_input.setMaximum(999999.99)
            sl_input.setValue(float(table.item(row, 5).text() or 0.0))
            layout.addWidget(QLabel("Stop Loss (SL):"))
            layout.addWidget(sl_input)
            tp_input = QDoubleSpinBox()
            tp_input.setDecimals(2)
            tp_input.setMinimum(0.0)
            tp_input.setMaximum(999999.99)
            tp_input.setValue(float(table.item(row, 6).text() or 0.0))
            layout.addWidget(QLabel("Take Profit (TP):"))
            layout.addWidget(tp_input)
            if "PENDING" in order_type.upper():
                vol_input = QDoubleSpinBox()
                vol_input.setDecimals(2)
                vol_input.setMinimum(0.01)
                vol_input.setMaximum(9999.99)
                vol_input.setValue(float(table.item(row, 3).text() or 0.0))
                price_input = QDoubleSpinBox()
                price_input.setDecimals(5)
                price_input.setMinimum(0.0)
                price_input.setMaximum(999999.99)
                price_input.setValue(float(table.item(row, 4).text() or 0.0))
                layout.addWidget(QLabel("Volume:"))
                layout.addWidget(vol_input)
                layout.addWidget(QLabel("Preço:"))
                layout.addWidget(price_input)
            else:
                vol_input = None
                price_input = None
            confirm_btn = QPushButton("Confirmar")
            confirm_btn.clicked.connect(lambda: self._send_modify_command(
                broker_key, ticket, symbol, sl_input.value(), tp_input.value(),
                vol_input.value() if vol_input else None,
                price_input.value() if price_input else None,
                modify_dialog,
                order_type
            ))
            layout.addWidget(confirm_btn)
            modify_dialog.show()

    def _send_modify_command(self, broker_key, ticket, symbol, sl, tp, volume=None, price=None, dialog=None, order_type=""):
        mode = self.broker_modes.get(broker_key, "Hedge")
        if mode == "Hedge" or "PENDING" in order_type.upper():
            command = "TRADE_POSITION_MODIFY" if "PENDING" not in order_type else "TRADE_ORDER_MODIFY"
            payload = {"ticket": int(ticket), "sl": sl, "tp": tp}
            if volume is not None:
                payload["volume"] = volume
            if price is not None:
                payload["price"] = price
        else:  # Netting
            command = "TRADE_POSITION_MODIFY"
            payload = {"ticket": int(ticket), "symbol": symbol, "sl": sl, "tp": tp}
        request_id = f"modify_{broker_key}_{int(time.time())}"
        self.pending_tickets[request_id] = ticket
        self._send_async_command(broker_key, command, payload, request_id)
        self.update_log(
            f"Comando enviado: Modificar ordem #{ticket} para {broker_key} às {time.strftime('%H:%M:%S', time.localtime())}.")
        logger.info(f"Comando agendado: Modificar ordem #{ticket} para {broker_key} com request_id {request_id}")
        if dialog:
            dialog.close()

    def _partial_close(self, row, broker_key):
        tab = self.broker_tabs.widget(self.broker_tabs.currentIndex())
        if tab and self.broker_tabs.tabText(self.broker_tabs.currentIndex()).startswith(broker_key):
            table = tab.layout().itemAt(0).widget()
            ticket = table.item(row, 0).text() if table.item(row, 0) else ""
            position_type = table.item(row, 2).text() if table.item(row, 2) else ""
            symbol = table.item(row, 1).text() if table.item(row, 1) else ""
            current_volume = float(table.item(row, 3).text() or 0.0)
            if not ticket or not position_type or not symbol:
                self.update_log("Erro: Informações da ordem não encontradas.")
                return
            partial_dialog = QDialog(self)
            partial_dialog.setWindowTitle(f"Fechamento Parcial - Ordem #{ticket}")
            layout = QVBoxLayout(partial_dialog)  # Corrigido: usar = para criar o layout
            volume_input = QDoubleSpinBox()
            volume_input.setDecimals(2)
            volume_input.setMinimum(0.01)
            volume_input.setMaximum(current_volume)
            volume_input.setValue(current_volume / 2)
            layout.addWidget(QLabel("Volume a Fechar:"))
            layout.addWidget(volume_input)
            confirm_btn = QPushButton("Confirmar")
            confirm_btn.clicked.connect(lambda: self._send_partial_command(
                broker_key, ticket, position_type, symbol, volume_input.value(), partial_dialog
            ))
            layout.addWidget(confirm_btn)
            partial_dialog.show()

    def _send_partial_command(self, broker_key, ticket, position_type, symbol, volume, dialog=None):
        mode = self.broker_modes.get(broker_key, "Hedge")
        if mode == "Hedge":
            command = "TRADE_POSITION_PARTIAL"
            payload = {"ticket": int(ticket), "volume": volume}
            request_id = f"partial_{broker_key}_{int(time.time())}"
            self.pending_tickets[request_id] = ticket
            self._send_async_command(broker_key, command, payload, request_id)
            self.update_log(
                f"Comando enviado: Fechamento parcial da ordem #{ticket} ({volume}) para {broker_key} às {time.strftime('%H:%M:%S', time.localtime())}.")
            logger.info(f"Comando agendado: Fechamento parcial da ordem #{ticket} para {broker_key} com request_id {request_id}")
        else:  # Netting
            opposite_type = "SELL" if position_type == "BUY" else "BUY"
            command = f"TRADE_ORDER_TYPE_{opposite_type}"
            payload = {"symbol": symbol, "type": opposite_type, "volume": volume}
            request_id = f"partial_netting_{broker_key}_{int(time.time())}"
            self.pending_tickets[request_id] = ticket
            self._send_async_command(broker_key, command, payload, request_id)
            self.update_log(
                f"Comando enviado: Redução de posição #{ticket} via ordem oposta ({opposite_type} {volume}) para {broker_key} às {time.strftime('%H:%M:%S', time.localtime())}.")
            logger.info(f"Comando agendado: Redução de posição #{ticket} para {broker_key} com request_id {request_id}")
        if dialog:
            dialog.close()

    @Slot(dict)
    def _update_trade_response(self, response):
        status = response.get("status", "unknown")
        message = response.get("message", "")
        broker_key = response.get("broker_key", "")
        request_id = response.get("request_id", "")
        if status == "OK":
            if any(key in request_id.lower() for key in ["close", "partial", "modify", "partial_netting"]):
                operation = "Fechada" if "close" in request_id.lower() else "Fechamento Parcial" if "partial" in request_id.lower() else "Modificada" if "modify" in request_id.lower() else "Reduzida"
                ticket = self.pending_tickets.get(request_id, "desconhecido")
                self.update_log(
                    f"{operation} ordem #{ticket} para {broker_key} às {time.strftime('%H:%M:%S', time.localtime())}.")
                logger.info(
                    f"Solicitando atualização de posições para {broker_key} após operação bem-sucedida com request_id {request_id}.")
                self._request_positions_for_broker(broker_key)
            else:
                self.update_log(f"Sucesso ({broker_key}): {message}")
                logger.debug(
                    f"Nenhuma atualização de posições solicitada para {broker_key}, request_id {request_id} não é operação de alteração.")
        else:
            error_message = response.get("error_message", "Erro desconhecido")
            self.update_log(f"Erro ({broker_key}): {error_message}")
            logger.error(f"Erro na resposta de {broker_key}: {error_message}")
        if request_id in self.pending_tickets:
            del self.pending_tickets[request_id]

    @Slot(str)
    def update_log(self, message):
        if any(key in message for key in ["Fechada", "Fechamento Parcial", "Modificada", "Reduzida", "Lista de posições atualizada", "Solicitando posições", "Erro"]):
            self.log_area.append(message)
            lines = self.log_area.toPlainText().split('\n')
            if len(lines) > 500:
                self.log_area.setText('\n'.join(lines[-500:]))
        logger.debug(f"Mensagem de log filtrada: {message}")

if __name__ == "__main__":
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    window = BoletaTraderGui(None, None, None, None, None)
    window.show()
    sys.exit(app.exec())

# Arquivo: gui/boleta_trader_gui.py
# Versão: 1.0.9.b - Envio 31