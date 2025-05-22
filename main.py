import sys
import asyncio
import platform
if platform.system() == "Windows":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        print("INFO: Política de loop de eventos do asyncio definida para WindowsSelectorEventLoopPolicy (Tentativa 2).")
    except Exception as e_policy:
        print(f"WARN: Falha ao definir WindowsSelectorEventLoopPolicy (Tentativa 2).")

# Elimina WARNING do PROACTOR (mas ele ainda está lá...)
import warnings
warnings.filterwarnings("ignore", category=RuntimeWarning, module="zmq.*")

import qasync
import logging
import signal
import functools
from PySide6.QtWidgets import QApplication
from core.config_manager import ConfigManager
from core.broker_manager import BrokerManager
from core.zmq_router import ZmqRouter
from gui.main_window import MainWindow
import os

# --- Adicionado código da SplashScreen ---
import tkinter as tk
from tkinter import ttk
import time

class SplashScreen:
    def __init__(self, duration=1.0):
        self.duration = duration
        self.root = tk.Tk()
        self.root.withdraw()

        self.splash = tk.Toplevel(self.root)
        self.splash.title("")
        self.splash.overrideredirect(True)

        # Dimensões e posição
        width, height = 400, 200
        screen_width = self.splash.winfo_screenwidth()
        screen_height = self.splash.winfo_screenheight()
        x = (screen_width - width) // 2
        y = (screen_height - height) // 2
        self.splash.geometry(f"{width}x{height}+{x}+{y}")

        # Conteúdo
        frame = ttk.Frame(self.splash)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            frame,
            text="Robot Trader MT5",
            font=("Arial", 24, "bold")
        ).pack(pady=(30, 10))

        ttk.Label(
            frame,
            text="Sistema de Trading Automatizado",
            font=("Arial", 12)
        ).pack(pady=5)

        ttk.Label(
            frame,
            text="Versão 1.0.9.b",
            font=("Arial", 10)
        ).pack(pady=5)

        self.progress = ttk.Progressbar(frame, mode="indeterminate", length=300)
        self.progress.pack(pady=20)
        self.progress.start()

        self.finished = False

    def close(self):
        if not self.finished:
            self.finished = True
            self.progress.stop()
            self.splash.destroy()
            self.root.destroy()

    def show(self, callback):
        def on_complete():
            if not self.finished:
                self.close()
                callback()

        self.root.after(int(self.duration * 1000), on_complete)
        self.root.mainloop()

# --- Fim do código da SplashScreen ---

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

# --- Funções de Encerramento (shutdown_cleanup, sigint_handler) ---
async def shutdown_cleanup():
    global zmq_task, zmq_router_instance, mt5_processes
    logger.info("Iniciando processo de limpeza (shutdown_cleanup)...")

    # Parar ZMQ Router
    if zmq_router_instance:
        logger.debug("Chamando zmq_router.stop()...")
        zmq_router_instance.stop()

    # Cancelar tarefa ZMQ
    if zmq_task and not zmq_task.done():
        logger.debug("Aguardando a tarefa ZMQ (_receive_loop) terminar...")
        try:
            await asyncio.wait_for(zmq_task, timeout=5.0)
            logger.info("Tarefa ZMQ finalizada graciosamente.")
        except asyncio.TimeoutError:
            logger.warning("Timeout ao esperar a tarefa ZMQ terminar. Cancelando...")
            zmq_task.cancel()
            try:
                await zmq_task
            except asyncio.CancelledError:
                logger.info("Tarefa ZMQ cancelada com sucesso após timeout.")
        except Exception as e:
             logger.exception(f"Erro ao esperar/cancelar tarefa ZMQ: {e}")
    else:
        logger.debug("Tarefa ZMQ já concluída ou não iniciada.")

    # Desconectar e parar processos MT5 gerenciados
    logger.info("Desconectando e parando processos MT5 gerenciados...")
    connected_brokers = broker_manager.get_connected_brokers()
    for key in connected_brokers:
        logger.info(f"Desconectando corretora {key}...")
        try:
            broker_manager.disconnect_broker(key)  # Chama a função de desconexão
            logger.info(f"Corretora {key} desconectada com sucesso.")
        except Exception as e:
            logger.error(f"Erro ao desconectar corretora {key}: {e}")

    # Parar processos MT5 gerenciados (garantia)
    logger.info("Parando processos MT5 gerenciados (garantia)...")
    for key, process in mt5_processes.items():
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
    await shutdown_cleanup()  # Chama a função de limpeza
    logger.info("Corrotina main() concluída.")
    app.quit()  # Garante que a aplicação Qt seja encerrada

# --- Bloco de Execução Principal (__main__) ---
if __name__ == "__main__":
    try:
        # --- Adicionado código da SplashScreen ---
        config = ConfigManager()
        show_splash_screen = config.getboolean('General', 'show_splash', fallback=True)
        if show_splash_screen:
            splash_duration = config.getfloat('General', 'splash_duration', fallback=1.0)
            splash = SplashScreen(splash_duration)
            splash.show(lambda: qasync.run(main()))
        else:
            qasync.run(main())
        # --- Fim do código da SplashScreen ---
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt pego no __main__ (inesperado).")
        if not shutdown_event.is_set():
            shutdown_event.set()
    except asyncio.CancelledError:
        logger.info("Execução cancelada no nível qasync (inesperado).")
    finally:
        logger.info("Aplicação encerrada completamente (bloco finally __main__).")
        sys.exit(0)  # Garante a saída do sistema

# ------------ término do arquivo main.py ------------
# Versão 1.0.9.b - envio 1