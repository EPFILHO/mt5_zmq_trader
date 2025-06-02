# main.py
# Versão 1.0.9.i - envio 8
# Ajustes:
# - CORREÇÃO CRÍTICA: Ajustada a ordem de inicialização e passagem de dependências
#   entre ZmqRouter e BrokerManager para resolver a dependência circular.
# - Adicionado filtro para o RuntimeWarning do Proactor event loop.
# - Implementado log colorido no console do PyCharm para melhor visualização.
# - Adicionado MT5ProcessMonitor para monitoramento e reinicialização de instâncias MT5.
# - Corrigido erro 'no running event loop' no MT5ProcessMonitor usando asyncio.run_coroutine_threadsafe.

import sys
import asyncio
import platform
import warnings
import qasync
import logging
import signal
import functools
import subprocess
import zmq.asyncio
from PySide6.QtWidgets import QApplication, QSplashScreen, QLabel, QVBoxLayout, QProgressBar, QWidget
from PySide6.QtGui import QFont
from PySide6.QtCore import Qt, QTimer
from core.config_manager import ConfigManager
from core.broker_manager import BrokerManager
from core.zmq_router import ZmqRouter
from core.mt5_process_monitor import MT5ProcessMonitor
from gui.main_window import MainWindow
import os
import time
from datetime import datetime

# Bloco 1 - Configuração Inicial e Patches
# Objetivo: Configurar políticas de loop de eventos, filtrar warnings e aplicar patches necessários.
# O logger é definido aqui para ser acessível globalmente no módulo.
logger = logging.getLogger(__name__)

def configure_asyncio_policy():
    """Configura a política de loop de eventos do asyncio para Windows."""
    if platform.system() == "Windows":
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            logger.info("Bloco 1 - Política de loop de eventos do asyncio definida para WindowsSelectorEventLoopPolicy.")
        except Exception as e_policy:
            logger.warning(f"Bloco 1 - Falha ao definir WindowsSelectorEventLoopPolicy: {e_policy}")

def filter_warnings():
    """Filtra warnings específicos para evitar poluição do log."""
    warnings.filterwarnings("ignore", category=RuntimeWarning, module="zmq.*")
    warnings.filterwarnings("ignore", message="not a socket")
    # Filtra o RuntimeWarning do Proactor event loop
    warnings.filterwarnings("ignore", message="Proactor event loop does not implement add_reader family of methods")
    logger.info("Bloco 1 - Filtros de warnings aplicados.")

# Bloco 2 - Patch para ZMQ
# Objetivo: Modificar o comportamento do método poll() do AsyncPoller para tratar o erro "not a socket".
original_asyncpoller_poll = zmq.asyncio.Poller.poll

async def patched_asyncpoller_poll(self, timeout=None):
    """Versão modificada do método poll() que trata o erro 'not a socket'."""
    try:
        return await original_asyncpoller_poll(self, timeout)
    except zmq.error.ZMQError as e:
        if "not a socket" in str(e):
            logger.info("Bloco 2 - Loop _receive_loop cancelado devido a 'not a socket' error.")
            return []
        else:
            raise

def apply_zmq_patch():
    """Aplica o patch ao método poll do zmq.asyncio.Poller."""
    zmq.asyncio.Poller.poll = patched_asyncpoller_poll
    logger.info("Bloco 2 - Patch ZMQ aplicado.")

# Bloco 3 - Classe SplashScreen
# Objetivo: Implementar uma tela de carregamento inicial para a aplicação.
class SplashScreen:
    def __init__(self, duration=1.0):
        self.duration = duration
        self.finished = False
        self.widget = QWidget()
        self.widget.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.widget.setFixedSize(400, 200)
        self.widget.setStyleSheet("background-color: white;")
        layout = QVBoxLayout(self.widget)
        title_label = QLabel("Robot Trader MT5")
        title_label.setFont(QFont("Arial", 24, QFont.Bold))
        title_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(title_label, 0, Qt.AlignCenter)
        subtitle_label = QLabel("Sistema de Trading Automatizado")
        subtitle_label.setFont(QFont("Arial", 12))
        subtitle_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(subtitle_label, 0, Qt.AlignCenter)
        version_label = QLabel("Versão 1.0.9")
        version_label.setFont(QFont("Arial", 10))
        version_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(version_label, 0, Qt.AlignCenter)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 0) # Modo indeterminado
        layout.addWidget(self.progress)
        layout.setContentsMargins(20, 30, 20, 20)
        layout.setSpacing(10)
        logger.info("Bloco 3 - SplashScreen inicializada.")

    def close(self):
        if not self.finished:
            self.finished = True
            self.widget.close()
            logger.debug("Bloco 3 - SplashScreen fechada.")

    def show(self, callback):
        screen = QApplication.primaryScreen().geometry()
        x = (screen.width() - self.widget.width()) // 2
        y = (screen.height() - self.widget.height()) // 2
        self.widget.move(x, y)
        self.widget.show()
        QTimer.singleShot(int(self.duration * 1000), lambda: self._on_complete(callback))
        logger.info("Bloco 3 - SplashScreen exibida.")

    def _on_complete(self, callback):
        if not self.finished:
            self.close()
            callback()
            logger.debug("Bloco 3 - SplashScreen completada.")

# Bloco 4 - Configuração de Logging
# Objetivo: Configurar o sistema de logging para criar um novo arquivo de log por execução e logs coloridos no console.
class ColoredFormatter(logging.Formatter):
    """
    Um formatador de log que adiciona cores às mensagens no console
    baseado no nível de log.
    """
    # Códigos de escape ANSI para cores
    GREY = "\x1b[38;20m"    # DEBUG
    BLUE = "\x1b[34;20m"    # INFO (usado para INFO, mas pode ser verde)
    YELLOW = "\x1b[33;20m"  # WARNING
    RED = "\x1b[31;20m"     # ERROR
    BOLD_RED = "\x1b[31;1m" # CRITICAL
    GREEN = "\x1b[32;20m"   # INFO (cor sugerida pelo usuário)
    RESET = "\x1b[0m"       # Reseta a cor

    FORMATS = {
        logging.DEBUG: BLUE + "%(asctime)s - %(levelname)s - %(filename)s - %(message)s" + RESET,
        logging.INFO: GREEN + "%(asctime)s - %(levelname)s - %(filename)s - %(message)s" + RESET,
        logging.WARNING: YELLOW + "%(asctime)s - %(levelname)s - %(filename)s - %(message)s" + RESET,
        logging.ERROR: RED + "%(asctime)s - %(levelname)s - %(filename)s - %(message)s" + RESET,
        logging.CRITICAL: BOLD_RED + "%(asctime)s - %(levelname)s - %(filename)s - %(message)s" + RESET
    }

    def format(self, record):
        log_fmt = self.FORMATS.get(record.levelno)
        formatter = logging.Formatter(log_fmt)
        return formatter.format(record)

def setup_logging():
    """Configura o sistema de logging com um arquivo de log único por execução e logs coloridos."""
    logs_dir = "logs"
    if not os.path.exists(logs_dir):
        os.makedirs(logs_dir)
        logger.info(f"Bloco 4 - Diretório de logs '{logs_dir}' criado.")

    # Gera um nome de arquivo de log único com timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_filename = os.path.join(logs_dir, f"robot_{timestamp}.log")

    # Carrega o nível de log do config.ini
    config_for_logging = ConfigManager()
    log_level_str = config_for_logging.get('General', 'log_level', fallback='INFO').upper()
    log_level = getattr(logging, log_level_str, logging.INFO)

    # Remove handlers existentes para evitar duplicação se a função for chamada mais de uma vez
    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)

    # Configura o handler de arquivo (sem cores)
    file_handler = logging.FileHandler(log_filename, mode='w', encoding='utf-8')
    file_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(filename)s - %(message)s"))
    logging.root.addHandler(file_handler)

    # Configura o handler de stream (console, com cores)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(ColoredFormatter())
    logging.root.addHandler(stream_handler)

    # Define o nível de log para o logger raiz
    logging.root.setLevel(log_level)
    logger.info(f"Bloco 4 - Logging configurado. Log file: {log_filename}")

# Bloco 5 - Evento Global e Variáveis de Encerramento
# Objetivo: Definir um evento global para sinalizar o encerramento da aplicação e variáveis globais.
shutdown_event = asyncio.Event()
zmq_task = None
zmq_router_instance = None
mt5_processes = {} # Adicionado para rastrear processos MT5
broker_manager = None
mt5_monitor = None
logger.info("Bloco 5 - Variáveis globais de encerramento inicializadas.")

# Bloco 6 - Funções de Encerramento
# Objetivo: Implementar a lógica de limpeza e parada controlada dos componentes da aplicação.
async def shutdown_cleanup():
    """Realiza a limpeza e encerramento de todos os componentes da aplicação."""
    global zmq_task, zmq_router_instance, mt5_processes, broker_manager, mt5_monitor
    logger.info("Bloco 6 - Iniciando processo de limpeza (shutdown_cleanup)....")

    # Para o monitor de processos MT5
    if mt5_monitor:
        logger.info("Bloco 6 - Parando MT5ProcessMonitor...")
        mt5_monitor.stop()
        logger.info("Bloco 6 - MT5ProcessMonitor parado.")

    # Parar ZMQ Router
    if zmq_router_instance:
        logger.debug("Bloco 6 - Chamando zmq_router.stop()...")
        try:
            await zmq_router_instance.stop()
        except Exception as e:
            logger.warning(f"Bloco 6 - Erro ao parar ZmqRouter: {e}")

    # Cancelar tarefa ZMQ
    if zmq_task and not zmq_task.done():
        logger.debug("Bloco 6 - Aguardando a tarefa ZMQ (_receive_loop) terminar...")
        try:
            # Reduzindo o timeout para evitar esperas longas
            await asyncio.wait_for(zmq_task, timeout=2.0)
            logger.info("Bloco 6 - Tarefa ZMQ finalizada graciosamente.")
        except asyncio.TimeoutError:
            logger.warning("Bloco 6 - Timeout ao esperar a tarefa ZMQ terminar. Cancelando...")
            try:
                zmq_task.cancel()
                # Não aguardar a tarefa após cancelamento para evitar erros
                logger.info("Bloco 6 - Tarefa ZMQ cancelada.")
            except Exception as e:
                logger.warning(f"Bloco 6 - Erro ao cancelar tarefa ZMQ: {e}")
        except Exception as e:
            logger.exception(f"Bloco 6 - Erro ao esperar/cancelar tarefa ZMQ: {e}")
    else:
        logger.debug("Bloco 6 - Tarefa ZMQ já concluída ou não iniciada.")

    # Desconectar e parar processos MT5 gerenciados
    logger.info("Bloco 6 - Desconectando e parando processos MT5 gerenciados...")
    if broker_manager: # Verificação de segurança
        try:
            connected_brokers = broker_manager.get_connected_brokers()
            for key in connected_brokers:
                logger.info(f"Bloco 6 - Desconectando corretora {key}...")
                try:
                    broker_manager.disconnect_broker(key) # Chama a função de desconexão
                    logger.info(f"Bloco 6 - Corretora {key} desconectada com sucesso.")
                except Exception as e:
                    logger.error(f"Bloco 6 - Erro ao desconectar corretora {key}: {e}")
        except Exception as e:
            logger.error(f"Bloco 6 - Erro ao obter corretoras conectadas: {e}")

    # Parar processos MT5 gerenciados (garantia)
    logger.info("Bloco 6 - Parando processos MT5 gerenciados (garantia)....")
    for key, process in list(mt5_processes.items()): # Usar uma cópia da lista para evitar modificação durante iteração
        logger.info(f"Bloco 6 - Parando MT5 para a corretora {key}: {key}...")
        try:
            process.terminate()
            process.wait(timeout=10) # Aumenta o timeout para 10 segundos
            logger.info(f"MT5 parado com sucesso para a corretora {key}.\n")
        except subprocess.TimeoutExpiredError:
            logger.warning(f"TimeoutExpired ao esperar MT5 para {key} terminar. Matando o processo...\n")
            process.kill()
        except Exception as e:
            logger.error(f"Erro ao parar MT5 para {key}: {e}\n")

    mt5_processes.clear() # Limpa o dicionário após parar os processos
    logger.info("Bloco 6 - Processo de limpeza (shutdown_cleanup) concluído.")

def sigint_handler(*args):
    """Manipulador de sinal SIGINT (Ctrl+C) para iniciar o encerramento."""
    logger.info("Bloco 6 - SIGINT (Ctrl+C) recebido. Sinalizando para encerrar...")
    if not shutdown_event.is_set():
        shutdown_event.set()

# Bloco 7 - Função Principal (main_application_flow)
# Objetivo: Orquestrar a inicialização dos componentes principais da aplicação.
async def main_application_flow():
    """Fluxo principal da aplicação, inicializando GUI, ZMQ e gerenciadores."""
    global zmq_task, zmq_router_instance, shutdown_event, mt5_processes, broker_manager, mt5_monitor
    logger.info("Bloco 7 - Iniciando o MT5 ZMQ Trader...")

    # --- Configurações Iniciais ---
    config = ConfigManager()
    base_mt5_path = config.get('General', 'base_mt5_path', fallback='C:/Temp/MT5')

    # Obtém o caminho da raiz do projeto
    root_path = os.path.dirname(os.path.abspath(__file__))

    # Inicializa ZmqRouter primeiro, pois BrokerManager precisa dele
    # Passa None temporariamente para broker_manager, será setado depois
    zmq_router_instance = ZmqRouter(None)

    # Inicializa BrokerManager, passando a instância do ZmqRouter
    broker_manager = BrokerManager(config, base_mt5_path, root_path, zmq_router_instance)

    # Agora que broker_manager está inicializado, podemos setá-lo no zmq_router_instance
    zmq_router_instance.broker_manager = broker_manager

    # Inicializa e inicia o monitor de processos MT5
    mt5_monitor = MT5ProcessMonitor(
        broker_manager,
        event_loop=asyncio.get_event_loop(),
        check_interval=config.getint('General', 'monitor_interval', fallback=10)
    )
    mt5_monitor.start()
    logger.info("Bloco 7 - MT5ProcessMonitor iniciado.")

    # --- Configuração QApplication e qasync Loop ---
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)

    # Configura o handler de SIGINT
    signal.signal(signal.SIGINT, sigint_handler)
    logger.debug("Handler de SIGINT configurado.")

    # --- Criação da GUI ---
    # Passa a referência do evento global para a MainWindow
    main_window = MainWindow(config, broker_manager, zmq_router_instance, shutdown_event, root_path)
    main_window.show()
    logger.info("Janela principal exibida.")

    # --- Inicia Tarefa ZMQ ---
    logger.info("Agendando ZMQ Router para iniciar com portas administrativas dinâmicas.")
    zmq_task = asyncio.create_task(zmq_router_instance.run(main_window.zmq_message_handler))

    # --- Dar chance para a tarefa ZMQ começar (await asyncio.sleep(0)) ---
    try:
        logger.debug("Cedendo controle ao loop asyncio (await asyncio.sleep(0))...")
        await asyncio.sleep(0)
        logger.debug("Controle retornado após sleep(0).")
    except asyncio.CancelledError:
        logger.warning("main_application_flow() cancelada durante o sleep(0) inicial.")

        # Se for cancelado aqui, garante que o evento seja setado para limpeza
        if not shutdown_event.is_set():
            shutdown_event.set()

    # --- Ponto de Espera Principal ---
    logger.info("Setup principal concluído. Aguardando sinal de encerramento (shutdown_event)...")
    try:
        await shutdown_event.wait() # Pausa main_application_flow() aqui até o evento ser setado
    except asyncio.CancelledError:
        logger.info("main_application_flow() foi cancelada enquanto esperava pelo shutdown_event.")

        # Ainda tenta limpar
        if not shutdown_event.is_set():
            shutdown_event.set()

    # --- Lógica de Encerramento (Após evento ser setado) ---
    logger.info("Sinal de encerramento recebido. Executando limpeza...")

    # Envolver o processo de limpeza em um bloco try-except para garantir que erros não interrompam o encerramento
    try:
        await shutdown_cleanup() # Chama a função de limpeza
    except Exception as e:
        logger.error(f"Erro durante o processo de limpeza: {e}")

    logger.info("Corrotina main_application_flow() concluída.")

    # Tentar garantir que a aplicação seja encerrada
    try:
        app.quit()
    except Exception as e:
        logger.error(f"Erro ao encerrar a aplicação Qt: {e}")

# Bloco 8 - Bloco de Execução Principal
# Objetivo principal: Gerenciar o ponto de entrada da aplicação, incluindo a SplashScreen e o loop principal.
if __name__ == "__main__":
    # Configurar logging o mais cedo possível para que todas as mensagens sejam capturadas
    setup_logging()
    logger.info("Iniciando execução.")

    # Executa as configurações e patches iniciais
    configure_asyncio_policy()
    filter_warnings()
    apply_zmq_patch()

    try:
        # Carrega a configuração para a splash screen
        config = ConfigManager()
        show_splash_screen = config.getboolean('General', 'show_splash', fallback=True)

        # Inicializar QApplication antes de qualquer coisa
        app = QApplication.instance()
        if app is None:
            app = QApplication(sys.argv)

        if show_splash_screen:
            splash_duration = config.getfloat('General', 'splash_duration', fallback=1.0)
            splash = SplashScreen(splash_duration)

            # Define a função que será chamada após a splash screen
            def start_main_app_flow_after_splash():
                try:
                    qasync.run(main_application_flow())
                except Exception as e:
                    logger.exception(f"Erro ao executar main_application_flow(): {e}")
                    sys.exit(1)

            # Exibe a splash screen e inicia o fluxo principal após o tempo definido
            splash.show(start_main_app_flow_after_splash)
            sys.exit(app.exec()) # Inicia o loop de eventos do Qt
        else:
            # Se não mostrar a splash screen, executar diretamente o fluxo principal
            qasync.run(main_application_flow())

    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt pego no __main__.")
        if not shutdown_event.is_set():
            shutdown_event.set()
    except asyncio.CancelledError:
        logger.info("Execução cancelada no nível qasync.")
    except Exception as e:
        logger.exception(f"Erro inesperado no bloco principal: {e}")
    finally:
        logger.info("Aplicação encerrada completamente (bloco finally __main__).")
        sys.exit(0)

# main.py
# Versão 1.0.9.i - envio 8