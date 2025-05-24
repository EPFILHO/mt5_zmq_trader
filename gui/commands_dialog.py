# gui/commands_dialog.py
import logging
import asyncio
import time
import json
import ctypes
from datetime import datetime
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QLineEdit,
    QPushButton, QMessageBox, QToolButton, QTextEdit
)
from PySide6.QtCore import Signal, Slot

logger = logging.getLogger(__name__)


class CommandsDialog(QDialog):
    def __init__(self, config, broker_manager, zmq_router, zmq_message_handler, main_window, parent=None):
        super().__init__(parent)
        self.config = config
        self.broker_manager = broker_manager
        self.zmq_router = zmq_router
        self.zmq_message_handler = zmq_message_handler
        self.main_window = main_window
        self.current_request_id = None  # Armazena o request_id do comando atual
        self.setWindowTitle("Boleta de Comandos")
        self.setMinimumWidth(600)
        self._init_ui()
        self._connect_signals()
        self._populate_brokers()
        self._update_buttons()

    def _init_ui(self):
        layout = QVBoxLayout(self)

        # Seleção de corretora
        select_layout = QHBoxLayout()
        select_label = QLabel("Selecionar corretora:")
        self.broker_combo = QComboBox()
        select_layout.addWidget(select_label)
        select_layout.addWidget(self.broker_combo)
        layout.addLayout(select_layout)

        # Informação do servidor
        self.server_label = QLabel("Servidor: N/A")
        layout.addWidget(self.server_label)

        # Botões de comando
        button_layout = QVBoxLayout()
        button_layout1 = QHBoxLayout()
        button_layout2 = QHBoxLayout()
        self.ping_button = QPushButton("PING")
        self.get_broker_info_button = QPushButton("Obter Info Corretora")
        self.get_account_info_button = QPushButton("Obter Info Conta")
        self.get_balance_button = QPushButton("Obter Saldo")
        self.get_leverage_button = QPushButton("Obter Alavancagem")
        self.get_flags_button = QPushButton("Obter Flags")
        self.get_margin_button = QPushButton("Obter Margem")
        self.get_time_button = QPushButton("Obter Tempo Servidor")

        button_layout1.addWidget(self.ping_button)
        button_layout1.addWidget(self.get_broker_info_button)
        button_layout1.addWidget(self.get_account_info_button)
        button_layout1.addWidget(self.get_balance_button)

        button_layout2.addWidget(self.get_leverage_button)
        button_layout2.addWidget(self.get_flags_button)
        button_layout2.addWidget(self.get_margin_button)
        button_layout2.addWidget(self.get_time_button)

        layout.addLayout(button_layout1)
        layout.addLayout(button_layout2)

        # Área de exibição de informações
        self.info_text_edit = QTextEdit()
        self.info_text_edit.setReadOnly(True)
        layout.addWidget(QLabel("Informações:"))
        layout.addWidget(self.info_text_edit)

        # Inicialmente desabilita todos os botões
        self.ping_button.setEnabled(False)
        self.get_broker_info_button.setEnabled(False)
        self.get_account_info_button.setEnabled(False)
        self.get_balance_button.setEnabled(False)
        self.get_leverage_button.setEnabled(False)
        self.get_flags_button.setEnabled(False)
        self.get_margin_button.setEnabled(False)
        self.get_time_button.setEnabled(False)

    def _connect_signals(self):
        self.broker_combo.currentIndexChanged.connect(self._update_buttons)
        self.broker_combo.currentIndexChanged.connect(self._update_server_label)
        self.ping_button.clicked.connect(self._on_ping_clicked)
        self.get_broker_info_button.clicked.connect(self._on_get_broker_info_clicked)
        self.get_account_info_button.clicked.connect(self._on_get_account_info_clicked)
        self.get_balance_button.clicked.connect(self._on_get_account_balance_clicked)
        self.get_leverage_button.clicked.connect(self._on_get_account_leverage_clicked)
        self.get_flags_button.clicked.connect(self._on_get_account_flags_clicked)
        self.get_margin_button.clicked.connect(self._on_get_account_margin_clicked)
        self.get_time_button.clicked.connect(self._on_get_time_server_clicked)
        self.zmq_message_handler.broker_info_received.connect(self._update_broker_info)
        self.zmq_message_handler.account_info_received.connect(self._update_account_info)
        self.zmq_message_handler.account_balance_received.connect(self._update_account_balance)
        self.zmq_message_handler.account_leverage_received.connect(self._update_account_leverage)
        self.zmq_message_handler.account_flags_received.connect(self._update_account_flags)
        self.zmq_message_handler.account_margin_received.connect(self._update_account_margin)
        self.zmq_message_handler.time_server_received.connect(self._update_time_server)
        self.zmq_message_handler.log_message_received.connect(self._on_log_message_received)
        self.main_window.broker_status_updated.connect(self._update_buttons)
        self.main_window.broker_connected.connect(self._select_broker)

    def _populate_brokers(self):
        self.broker_combo.clear()
        connected_brokers = self.broker_manager.get_connected_brokers()
        brokers = self.broker_manager.get_brokers()
        logger.debug(f"Populando QComboBox com corretoras conectadas: {connected_brokers}")
        for key in sorted(brokers.keys()):
            if key in connected_brokers:
                self.broker_combo.addItem(key)
        self._update_buttons()
        logger.info(
            f"Lista de corretoras atualizada na QComboBox: {[self.broker_combo.itemText(i) for i in range(self.broker_combo.count())]}")

    @Slot(str)
    def _select_broker(self, broker_key: str):
        """Seleciona a corretora recém-conectada na QComboBox."""
        index = self.broker_combo.findText(broker_key)
        if index >= 0:
            self.broker_combo.setCurrentIndex(index)
            logger.info(f"Corretora {broker_key} selecionada automaticamente na QComboBox.")
        else:
            logger.debug(f"Corretora {broker_key} não encontrada na QComboBox.")

    def _update_buttons(self):
        selected_key = self.broker_combo.currentText()
        if selected_key and selected_key in self.main_window.broker_status:
            is_registered = self.main_window.broker_status[selected_key]
        else:
            is_registered = False

        self.ping_button.setEnabled(is_registered)
        self.get_broker_info_button.setEnabled(is_registered)
        self.get_account_info_button.setEnabled(is_registered)
        self.get_balance_button.setEnabled(is_registered)
        self.get_leverage_button.setEnabled(is_registered)
        self.get_flags_button.setEnabled(is_registered)
        self.get_margin_button.setEnabled(is_registered)
        self.get_time_button.setEnabled(is_registered)
        #self.info_text_edit.clear()  # Limpa o info_text_edit ao mudar a corretora
        logger.debug(
            f"Botões atualizados para corretora {selected_key}. Registrada: {is_registered}. Caixa de texto limpa.")

    @Slot()
    def _update_server_label(self):
        """Atualiza o label do servidor com base na corretora selecionada."""
        selected_key = self.broker_combo.currentText()
        if selected_key:
            brokers = self.broker_manager.get_brokers()
            server = brokers[selected_key].get("server", "N/A")
            self.server_label.setText(f"Servidor: {server}")
        else:
            self.server_label.setText("Servidor: N/A")

    async def _send_command(self, command):
        broker_key = self.broker_combo.currentText()
        if not broker_key:
            self.info_text_edit.append("Erro: Nenhuma corretora selecionada.")
            logger.warning("Nenhuma corretora selecionada ao tentar enviar comando.")
            return
        self.current_request_id = f"{command.lower()}_{broker_key}_{int(time.time())}"
        try:
            response = await self.zmq_router.send_command_to_broker(
                broker_key, command, {}, self.current_request_id
            )
            if isinstance(response, dict):
                if response.get("status") == "ERROR":
                    self.info_text_edit.append(f"Erro: {response.get('message', 'Falha desconhecida')}")
                    logger.error(f"Falha ao enviar {command} para {broker_key}: {response.get('message')}")
                else:
                    # Resposta será exibida pelos métodos _update_* via sinais
                    logger.info(f"Resposta recebida para {command} de {broker_key}: {response}")
            else:
                self.info_text_edit.append(f"Erro: Resposta inválida para {command}.")
                logger.error(f"Resposta inválida para {command} de {broker_key}: {response}")
        except asyncio.TimeoutError:
            self.info_text_edit.append(f"Erro: Timeout ao aguardar resposta para {command}.")
            logger.error(f"Timeout ao enviar {command} para {broker_key}")
        except Exception as e:
            self.info_text_edit.append(f"Erro ao enviar comando: {str(e)}")
            logger.error(f"Exceção ao enviar {command} para {broker_key}: {str(e)}")

    @Slot()
    def _on_ping_clicked(self):
        broker_key = self.broker_combo.currentText()
        if not broker_key:
            self.info_text_edit.append("Erro: Nenhuma corretora selecionada.")
            logger.warning("Nenhuma corretora selecionada para PING.")
            return
        self.info_text_edit.clear()  # Limpa o info_text_edit antes de enviar o PING
        self.current_request_id = f"ping_{broker_key}_{int(time.time())}"
        self.zmq_message_handler.send_ping(broker_key)
        logger.debug(f"Enviando PING para {broker_key}. Caixa de texto limpa.")

    @Slot()
    def _on_get_broker_info_clicked(self):
        asyncio.create_task(self._send_command("GET_BROKER_INFO"))

    @Slot()
    def _on_get_account_info_clicked(self):
        asyncio.create_task(self._send_command("GET_ACCOUNT_INFO"))

    @Slot()
    def _on_get_account_balance_clicked(self):
        asyncio.create_task(self._send_command("GET_ACCOUNT_BALANCE"))

    @Slot()
    def _on_get_account_leverage_clicked(self):
        asyncio.create_task(self._send_command("GET_ACCOUNT_LEVERAGE"))

    @Slot()
    def _on_get_account_flags_clicked(self):
        asyncio.create_task(self._send_command("GET_ACCOUNT_FLAGS"))

    @Slot()
    def _on_get_account_margin_clicked(self):
        asyncio.create_task(self._send_command("GET_ACCOUNT_MARGIN"))

    @Slot()
    def _on_get_time_server_clicked(self):
        asyncio.create_task(self._send_command("GET_TIME_SERVER"))

    @Slot(str)
    def _on_log_message_received(self, message):
        # Filtrar mensagens indesejadas, exceto mensagens de registro
        if any(keyword in message for keyword in ["Heartbeat", "ZMQ RX", "Resposta OK recebida", "desconectada"]):
            logger.debug(f"Mensagem filtrada (não exibida no info_text_edit): {message}")
            return
        self.info_text_edit.append(message)
        logger.debug(f"Mensagem de log exibida: {message}")

    @Slot(dict)
    def _update_broker_info(self, broker_info):
        text = f"Corretora: {broker_info.get('company', 'N/A')}\n"
        self.info_text_edit.setText(text)
        logger.debug(f"Informações da corretora atualizadas: {text}")

    @Slot(dict)
    def _update_account_info(self, account_info):
        text = f"Login: {account_info.get('login', 'N/A')}\n"
        text += f"Nome: {account_info.get('name', 'N/A')}\n"
        self.info_text_edit.setText(text)
        logger.debug(f"Informações da conta atualizadas: {text}")

    @Slot(dict)
    def _update_account_balance(self, account_balance):
        text = f"Balanço: {account_balance.get('balance', 'N/A')}\n"
        text += f"Equity: {account_balance.get('equity', 'N/A')}\n"
        text += f"Moeda: {account_balance.get('currency', 'N/A')}\n"
        self.info_text_edit.setText(text)
        logger.debug(f"Saldo da conta atualizado: {text}")

    @Slot(dict)
    def _update_account_leverage(self, account_leverage):
        text = f"Alavancagem: {account_leverage.get('leverage', 'N/A')}\n"
        self.info_text_edit.setText(text)
        logger.debug(f"Alavancagem da conta atualizada: {text}")

    @Slot(dict)
    def _update_account_flags(self, account_flags):
        text = f"Algotrading Habilitado: {account_flags.get('trade_allowed', 'N/A')}\n"
        text += f"Negociação Permitida: {account_flags.get('expert_enabled', 'N/A')}\n"
        self.info_text_edit.setText(text)
        logger.debug(f"Flags da conta atualizados: {text}")

    @Slot(dict)
    def _update_account_margin(self, account_margin):
        text = f"Margem: {account_margin.get('margin', 'N/A')}\n"
        text += f"Margem Livre: {account_margin.get('free_margin', 'N/A')}\n"
        text += f"Nível de Margem: {account_margin.get('margin_level', 'N/A')}\n"
        self.info_text_edit.setText(text)
        logger.debug(f"Margem da conta atualizada: {text}")

    # --- Função para pegar hora local do Windows (sempre atualizada) ---
    def get_windows_local_time(self):
        class SYSTEMTIME(ctypes.Structure):
            _fields_ = [
                ("wYear", ctypes.c_ushort),
                ("wMonth", ctypes.c_ushort),
                ("wDayOfWeek", ctypes.c_ushort),
                ("wDay", ctypes.c_ushort),
                ("wHour", ctypes.c_ushort),
                ("wMinute", ctypes.c_ushort),
                ("wSecond", ctypes.c_ushort),
                ("wMilliseconds", ctypes.c_ushort),
            ]

        system_time = SYSTEMTIME()
        ctypes.windll.kernel32.GetLocalTime(ctypes.byref(system_time))
        return datetime(
            system_time.wYear, system_time.wMonth, system_time.wDay,
            system_time.wHour, system_time.wMinute, system_time.wSecond, system_time.wMilliseconds * 1000
        )

    @Slot(dict)
    def _update_time_server(self, time_server):
        server_time = time_server.get('time_server', None)
        if server_time is None or server_time == 'N/A':
            text = "Tempo do Servidor: N/A\n"
        else:
            try:
                server_time_float = float(server_time)

                # 1. Tempo do Servidor (UTC)
                server_dt_utc = datetime.utcfromtimestamp(server_time_float)
                server_time_utc_str = server_dt_utc.strftime('%Y-%m-%d %H:%M:%S (UTC)')
                text = f"Tempo do Servidor (UTC): {server_time_utc_str}\n"

                # 2. Hora Local do Windows (sempre atualizado)
                local_dt = self.get_windows_local_time()
                local_time_str = local_dt.strftime('%Y-%m-%d %H:%M:%S')
                text += f"Hora Local (Windows): {local_time_str}\n"

                # 3. Diferença correta: entre o tempo do servidor (UTC) e o horário local do Windows
                time_diff = server_dt_utc - local_dt
                time_diff_seconds = time_diff.total_seconds()
                logger.debug(
                    f"server_dt_utc: {server_dt_utc}, local_dt: {local_dt}, time_diff: {time_diff_seconds} segundos")
                time_diff_rounded = round(time_diff_seconds)
                hours, remainder = divmod(abs(time_diff_rounded), 3600)
                minutes, seconds = divmod(remainder, 60)
                sign = '+' if time_diff_rounded >= 0 else '-'
                time_diff_str = f"{sign}{int(hours)} horas, {int(minutes)} minutos, {int(seconds)} segundos"
                text += f"Diferença para Hora Local: {time_diff_str}\n"
                logger.debug(
                    f"Conversão de time_diff: {time_diff_seconds} segundos -> arredondado: {time_diff_rounded} segundos -> {time_diff_str}")

            except (ValueError, TypeError) as e:
                text = f"Tempo do Servidor: {server_time} (formato inválido)\n"
                logger.error(f"Erro ao formatar time_server: {server_time}. Erro: {str(e)}")
        self.info_text_edit.setText(text)
        logger.debug(f"Tempo do servidor atualizado: {text}")

    def update_brokers(self):
        """Atualiza a lista de corretoras conectadas."""
        self._populate_brokers()

        # Versão 1.0.6 - envio 8  - GROK (envio final - ajuda da Perplexity até o envio 7)


# ------------ término do arquivo commands_dialog.py ------------
# Versão 1.0.8 - envio 1