# internet_monitor.py
import time
import logging
import threading
import socket
import psutil

logger = logging.getLogger(__name__)

class InternetMonitor:
    def __init__(self, status_callback, check_interval=5):
        """
        Inicializa o monitor de conexão com a internet.

        Args:
            status_callback (callable): Função para atualizar o status na GUI.
            check_interval (int): Intervalo de verificação em segundos.
        """
        self.status_callback = status_callback
        self.check_interval = check_interval
        self.running = False
        self.monitor_thread = None
        self.internet_status = False
        self.last_cpu = 0
        self.last_memory = 0

    def is_online(self):
        """
        Verifica se há conexão com a internet.

        Returns:
            bool: True se online, False caso contrário.
        """
        try:
            socket.create_connection(("8.8.8.8", 53), timeout=3)
            return True
        except OSError:
            return False

    def get_system_info(self):
        """
        Obtém informações de CPU e memória.

        Returns:
            tuple: Percentual de uso de CPU e memória.
        """
        try:
            cpu = psutil.cpu_percent(interval=0.1)
            memory = psutil.virtual_memory().percent
            return cpu, memory
        except Exception as e:
            logging.error(f"Erro ao obter informações do sistema: {e}")
            return 0, 0

    def start(self):
        """
        Inicia a thread de monitoramento.
        """
        if not self.monitor_thread or not self.monitor_thread.is_alive():
            self.running = True
            self.monitor_thread = threading.Thread(target=self.monitor, daemon=True)
            self.monitor_thread.start()
            logging.info("InternetMonitor iniciado")
        else:
            logging.warning("InternetMonitor já está em execução")

    def stop(self):
        """
        Para a thread de monitoramento.
        """
        if self.running:
            self.running = False
            if self.monitor_thread and self.monitor_thread.is_alive():
                self.monitor_thread.join(timeout=7)  # Aumentado para 7 segundos
                if self.monitor_thread.is_alive():
                    logging.debug("Thread de InternetMonitor ainda ativa, tentando join adicional")
                    self.monitor_thread.join(timeout=1)  # Segunda tentativa com 1 segundo
                    if self.monitor_thread.is_alive():
                        logging.warning("Thread de InternetMonitor não terminou após join adicional")
                    else:
                        logging.info("Thread de InternetMonitor encerrada com sucesso após join adicional")
                else:
                    logging.info("Thread de InternetMonitor encerrada com sucesso")
            logging.info("InternetMonitor parado")

    def monitor(self):
        """
        Monitora continuamente a conexão com a internet e informações do sistema.
        """
        while self.running:
            logging.debug("Monitoramento em execução...")  # Adicionando log para debug
            try:
                self.update_status()
            except Exception as e:
                logging.error(f"Erro no monitoramento de internet: {e}")
            time.sleep(self.check_interval)
        logging.debug("Monitoramento encerrado.")  # Adicionando log para debug

    def update_status(self):
        """
        Atualiza o status da conexão e informações do sistema.
        """
        new_status = self.is_online()
        cpu, memory = self.get_system_info()

        if new_status != self.internet_status:
            self.internet_status = new_status
            logging.info(f" Internet {'Online' if new_status else 'Offline'}")

        status = {
            "internet": "Online" if self.internet_status else "Offline",
            "cpu": f" CPU: {cpu:.1f}%",
            "memory": f" Memória: {memory:.1f}%"
        }

        try:
            self.status_callback(status)
        except Exception as e:
            logging.warning(f"Erro ao atualizar status na GUI: {e}")

# Versão 1.0.9.i - envio 1 - Alteração de Término (join)