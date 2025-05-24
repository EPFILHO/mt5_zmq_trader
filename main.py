import sys
import asyncio
import platform

if platform.system() == "Windows":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        print(
            "INFO: Política de loop de eventos do asyncio definida para WindowsSelectorEventLoopPolicy (Tentativa 2).")
    except Exception as e_policy:
        print(f"WARN: Falha ao definir WindowsSelectorEventLoopPolicy (Tentativa 2).")

# Elimina WARNING do PROACTOR (mas ele ainda está lá...)
import warnings

warnings.filterwarnings("ignore", category=RuntimeWarning, module="zmq.*")
# Adicionar filtro específico para o erro "not a socket" durante o encerramento
warnings.filterwarnings("ignore", message="not a socket")

import qasync
import logging
import signal
import functools
import subprocess
import zmq.asyncio
from PySide6.QtWidgets import QApplication, QSplashScreen, QLabel, QVBoxLayout, QProgressBar, QWidget
from PySide6.QtGui import QPixmap, QFont
from PySide6.QtCore import Qt, QTimer
from core.config_manager import ConfigManager
from core.broker_manager import BrokerManager
from core.zmq_router import ZmqRouter
from gui.main_window import MainWindow
import os
import time  # Mantido para compatibilidade

# Patch para o método _receive_loop do ZmqRouter
# Vamos modificar o comportamento do método poll() do AsyncPoller para tratar o erro "not a socket"
original_asyncpoller_poll = zmq.asyncio.Poller.poll


async def patched_asyncpoller_poll(self, timeout=None):
    """Versão modificada do método poll() que trata o erro 'not a socket'"""
    try:
        return await original_asyncpoller_poll(self, timeout)
    except zmq.error.ZMQError as e:
        if "not a socket" in str(e):
            # Se o erro for "not a socket", retornamos uma lista vazia em vez de lançar exceção
            logger = logging.getLogger(__name__)
            logger.info("Loop _receive_loop cancelado.")
            return []
        else:
            # Para outros erros, relançamos a exceção
            raise


# Aplicar o patch
zmq.asyncio.Poller.poll = patched_asyncpoller_poll


# --- SplashScreen com PySide6/Qt ---
class SplashScreen:
    def __init__(self, duration=1.0):
        self.duration = duration
        self.finished = False

        # Criar widget base
        self.widget = QWidget()
        self.widget.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.widget.setFixedSize(400, 200)
        self.widget.setStyleSheet("background-color: white;")

        # Layout
        layout = QVBoxLayout(self.widget)

        # Título
        title_label = QLabel("Robot Trader MT5")
        title_label.setFont(QFont("Arial", 24, QFont.Bold))
        title_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(title_label, 0, Qt.AlignCenter)

        # Subtítulo
        subtitle_label = QLabel("Sistema de Trading Automatizado")
        subtitle_label.setFont(QFont("Arial", 12))
        subtitle_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(subtitle_label, 0, Qt.AlignCenter)

        # Versão
        version_label = QLabel("Versão 1.0.9.c")
        version_label.setFont(QFont("Arial", 10))
        version_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(version_label, 0, Qt.AlignCenter)

        # Barra de progresso
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 0)  # Modo indeterminado
        layout.addWidget(self.progress)

        # Configurar espaçamento
        layout.setContentsMargins(20, 30, 20, 20)
        layout.setSpacing(10)

    def close(self):
        if not self.finished:
            self.finished = True
            self.widget.close()

    def show(self, callback):
        # Centralizar na tela
        screen = QApplication.primaryScreen().geometry()
        x = (screen.width() - self.widget.width()) // 2
        y = (screen.height() - self.widget.height()) // 2
        self.widget.move(x, y)

        # Mostrar o widget
        self.widget.show()

        # Configurar timer para fechar após a duração
        QTimer.singleShot(int(self.duration * 1000), lambda: self._on_complete(callback))

    def _on_complete(self, callback):
        if not self.finished:
            self.close()
            callback()


# --- Configuração de Logging baseada no config.ini ---
# Carrega o config para pegar o nível de log antes de configurar o logging
config_for_logging = ConfigManager()
log_level_str = config_for_logging.get('General', 'log_level', fallback='INFO').upper()
log_level = getattr(logging, log_level_str, logging.INFO)

logging.basicConfig(
    level=log_level,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("logs/robot.log", mode='a'),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)
# --- FIM DA ALTERAÇÃO NO LOGGING ---

# --- Evento Global para Encerramento ---
shutdown_event = asyncio.Event()

# --- Variáveis Globais para acesso no encerramento ---
zmq_task = None
zmq_router_instance = None
mt5_processes = {}  # Adicionado para rastrear processos MT5
broker_manager = None


# --- Funções de Encerramento (shutdown_cleanup, sigint_handler) ---
async def shutdown_cleanup():
    global zmq_task, zmq_router_instance, mt5_processes, broker_manager

    logger.info("Iniciando processo de limpeza (shutdown_cleanup)...")

    # Parar ZMQ Router
    if zmq_router_instance:
        logger.debug("Chamando zmq_router.stop()...")
        try:
            zmq_router_instance.stop()
        except Exception as e:
            logger.warning(f"Erro ao parar ZmqRouter: {e}")

    # Cancelar tarefa ZMQ
    if zmq_task and not zmq_task.done():
        logger.debug("Aguardando a tarefa ZMQ (_receive_loop) terminar...")
        try:
            # Reduzindo o timeout para evitar esperas longas
            await asyncio.wait_for(zmq_task, timeout=2.0)
            logger.info("Tarefa ZMQ finalizada graciosamente.")
        except asyncio.TimeoutError:
            logger.warning("Timeout ao esperar a tarefa ZMQ terminar. Cancelando...")
            try:
                zmq_task.cancel()
                # Não aguardar a tarefa após cancelamento para evitar erros
                logger.info("Tarefa ZMQ cancelada.")
            except Exception as e:
                logger.warning(f"Erro ao cancelar tarefa ZMQ: {e}")
        except Exception as e:
            logger.exception(f"Erro ao esperar/cancelar tarefa ZMQ: {e}")
    else:
        logger.debug("Tarefa ZMQ já concluída ou não iniciada.")

    # Desconectar e parar processos MT5 gerenciados
    logger.info("Desconectando e parando processos MT5 gerenciados...")
    if broker_manager:  # Verificação de segurança
        try:
            connected_brokers = broker_manager.get_connected_brokers()
            for key in connected_brokers:
                logger.info(f"Desconectando corretora {key}...")
                try:
                    broker_manager.disconnect_broker(key)  # Chama a função de desconexão
                    logger.info(f"Corretora {key} desconectada com sucesso.")
                except Exception as e:
                    logger.error(f"Erro ao desconectar corretora {key}: {e}")
        except Exception as e:
            logger.error(f"Erro ao obter corretoras conectadas: {e}")
    else:
        logger.warning("broker_manager não está inicializado durante o cleanup")

    # Parar processos MT5 gerenciados (garantia)
    logger.info("Parando processos MT5 gerenciados (garantia)...")
    for key, process in list(mt5_processes.items()):  # Usar uma cópia da lista para evitar modificação durante iteração
        logger.info(f"Parando MT5 para a corretora {key}...")
        try:
            process.terminate()
            process.wait(timeout=10)  # Aumenta o timeout para 10 segundos
            logger.info(f"MT5 parado com sucesso para a corretora {key}.")
        except subprocess.TimeoutExpired:
            logger.warning(f"Timeout ao esperar MT5 para {key} terminar. Matando o processo...")
            process.kill()
        except Exception as e:
            logger.error(f"Erro ao parar MT5 para a corretora {key}: {e}")

    mt5_processes.clear()  # Limpa o dicionário após parar os processos

    logger.info("Processo de limpeza (shutdown_cleanup) concluído.")


def sigint_handler(*args):
    logger.info("SIGINT (Ctrl+C) recebido. Sinalizando para encerrar...")
    if not shutdown_event.is_set():
        shutdown_event.set()


# --- Função Principal (main) ---
async def main():
    global zmq_task, zmq_router_instance, shutdown_event, mt5_processes, broker_manager

    logger.info("Iniciando o MT5 ZMQ Trader...")

    # --- Configurações Iniciais ---
    config = ConfigManager()
    base_mt5_path = config.get('General', 'base_mt5_path', fallback='C:/Program Files/MetaTrader 5')

    # Obtém o caminho da raiz do projeto
    root_path = os.path.dirname(os.path.abspath(__file__))

    broker_manager = BrokerManager(config, base_mt5_path, root_path)

    # Usar a instância global para acesso na limpeza
    zmq_router_instance = ZmqRouter(broker_manager)  # Ajustado para passar BrokerManager

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
    logger.info("Agendando ZMQ Router para iniciar com portas administrativas dinâmicas")
    zmq_task = asyncio.create_task(zmq_router_instance.run(main_window.zmq_message_handler))

    # --- Dar chance para a tarefa ZMQ começar (await asyncio.sleep(0)) ---
    try:
        logger.debug("Cedendo controle ao loop asyncio (await asyncio.sleep(0))...")
        await asyncio.sleep(0)
        logger.debug("Controle retornado após sleep(0).")
    except asyncio.CancelledError:
        logger.warning("main() cancelada durante o sleep(0) inicial.")

        # Se for cancelado aqui, garante que o evento seja setado para limpeza
        if not shutdown_event.is_set():
            shutdown_event.set()

    # --- Ponto de Espera Principal ---
    logger.info("Setup principal concluído. Aguardando sinal de encerramento (shutdown_event)...")
    try:
        await shutdown_event.wait()  # Pausa main() aqui até o evento ser setado
    except asyncio.CancelledError:
        logger.info("main() foi cancelada enquanto esperava pelo shutdown_event.")

        # Ainda tenta limpar
        if not shutdown_event.is_set():
            shutdown_event.set()  # Garante que a limpeza ocorra

    # --- Lógica de Encerramento (Após evento ser setado) ---
    logger.info("Sinal de encerramento recebido. Executando limpeza...")

    # Envolver o processo de limpeza em um bloco try-except para garantir que erros não interrompam o encerramento
    try:
        await shutdown_cleanup()  # Chama a função de limpeza
    except Exception as e:
        logger.error(f"Erro durante o processo de limpeza: {e}")

    logger.info("Corrotina main() concluída.")

    # Tentar garantir que a aplicação seja encerrada
    try:
        app.quit()  # Garante que a aplicação Qt seja encerrada
    except Exception as e:
        logger.error(f"Erro ao encerrar a aplicação Qt: {e}")


# --- Bloco de Execução Principal (__main__) ---
if __name__ == "__main__":
    try:
        # --- Código da SplashScreen ---
        config = ConfigManager()
        show_splash_screen = config.getboolean('General', 'show_splash', fallback=True)

        # Inicializar QApplication antes de qualquer coisa
        app = QApplication.instance()
        if app is None:
            app = QApplication(sys.argv)

        if show_splash_screen:
            splash_duration = config.getfloat('General', 'splash_duration', fallback=1.0)
            splash = SplashScreen(splash_duration)


            # Executar o loop de eventos Qt com a splash screen
            def start_main():
                # Usar o qasync para executar a função principal
                try:
                    qasync.run(main())
                except Exception as e:
                    logger.exception(f"Erro ao executar main(): {e}")
                    sys.exit(1)


            splash.show(start_main)
            sys.exit(app.exec())
        else:
            # Se não mostrar a splash screen, executar diretamente
            qasync.run(main())

    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt pego no __main__ (inesperado).")
        if not shutdown_event.is_set():
            shutdown_event.set()
    except asyncio.CancelledError:
        logger.info("Execução cancelada no nível qasync (inesperado).")
    except Exception as e:
        logger.exception(f"Erro inesperado no bloco principal: {e}")
    finally:
        logger.info("Aplicação encerrada completamente (bloco finally __main__).")
        sys.exit(0)  # Garante a saída do sistema

# ------------ término do arquivo main.py ------------
# Versão 1.0.9.c - envio 6