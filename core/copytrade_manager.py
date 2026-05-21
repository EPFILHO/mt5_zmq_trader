# EPCopyFlow 2.0 - Versão 0.0.1 - Claude Code Parte 000
# core/copytrade_manager.py
# Gerenciador de copytrade: recebe eventos do Master e replica para Slaves.
# Persistência em SQLite para histórico de cópias.

import sqlite3
import time
import math
import logging
import asyncio
import hashlib
import datetime
from PySide6.QtCore import QObject, Signal

logger = logging.getLogger(__name__)

DB_FILE = "copytrade_history.db"


class CopyTradeManager(QObject):
    copy_trade_executed = Signal(dict)
    copy_trade_failed = Signal(dict)
    copy_trade_log = Signal(str)
    emergency_completed = Signal(bool, str)  # (success, message)

    # GUI chama request_*(), motor executa _fetch_*() e emite o sinal
    # cross-thread; slot na main thread atualiza UI.
    trade_history_ready = Signal(list, str)   # (rows, broker_key_filter)
    today_stats_ready = Signal(dict)          # {"total", "success", "failed"}
    history_cleared = Signal(int)             # nº de registros removidos

    # ──────────────────────────────────────────────
    # Bloco 1 - Inicialização e Banco de Dados
    # ──────────────────────────────────────────────
    def __init__(self, broker_manager, tcp_router, parent=None):
        super().__init__(parent)
        self.broker_manager = broker_manager
        self.tcp_router = tcp_router
        self.position_map = {}  # position_id (POSITION_IDENTIFIER) -> {slave_key: slave_ticket}
        self._emergency_active = False  # Suprime replicação durante emergency close
        self._emergency_completed_at = 0  # Timestamp do fim do emergency (grace period)
        self.symbol_specs_cache = {}  # (broker_key, symbol) -> {volume_min, volume_max, volume_step}
        self._position_locks = {}  # position_id -> asyncio.Lock (serializa eventos do mesmo position_id)
        self._master_event_dedup = {}  # (position_id, timestamp_mql) -> process_time

        # SQLite confinado à thread do motor: connection nasce em
        # bootstrap_engine, leituras da GUI vão via request_*() + signal.
        # Engine é single-threaded asyncio, então zero locks.
        self.db = sqlite3.connect(DB_FILE)
        self._init_db()

        self.engine = None  # wired pelo main.py após bootstrap_engine.

        logger.info("CopyTradeManager inicializado.")

    def _init_db(self):
        # Tabela original: histórico de replicações
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS copytrade_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                master_broker TEXT NOT NULL,
                master_ticket INTEGER,
                symbol TEXT NOT NULL,
                action TEXT NOT NULL,
                master_lot REAL NOT NULL,
                slave_broker TEXT NOT NULL,
                slave_ticket INTEGER,
                slave_lot REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                error_message TEXT DEFAULT ''
            )
        """)

        # Tabela: Posições abertas (rastreamento ativo)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS open_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                master_ticket INTEGER NOT NULL,
                slave_broker TEXT NOT NULL,
                slave_ticket INTEGER,
                symbol TEXT NOT NULL,

                master_volume_original REAL NOT NULL,
                master_volume_current REAL NOT NULL,
                slave_volume_current REAL NOT NULL,

                status TEXT NOT NULL,  -- SYNCING / OPEN / SYNCED / CLOSING / CLOSED
                request_id TEXT UNIQUE,

                opened_at REAL NOT NULL,
                synced_at REAL,
                last_heartbeat REAL,
                closed_at REAL,

                sync_attempts INTEGER DEFAULT 0,
                last_error TEXT,

                UNIQUE(master_ticket, slave_broker)
            )
        """)

        # Tabela: Status de cada slave (paused/active)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS slave_status (
                slave_broker TEXT PRIMARY KEY,
                status TEXT NOT NULL,  -- ACTIVE / PAUSED
                paused_reason TEXT,
                paused_at REAL,
                paused_by_ticket INTEGER,
                can_resume_at REAL,
                last_heartbeat REAL
            )
        """)

        # Tabela: Estado do master (fonte de verdade independente dos slaves)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS master_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                master_broker TEXT NOT NULL,
                position_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                volume REAL NOT NULL,
                volume_original REAL NOT NULL,
                sl REAL DEFAULT 0.0,
                tp REAL DEFAULT 0.0,
                status TEXT NOT NULL DEFAULT 'OPEN',
                opened_at REAL NOT NULL,
                closed_at REAL,
                UNIQUE(master_broker, position_id)
            )
        """)

        self.db.commit()

        # Migrações: adicionar colunas novas se não existirem
        migrations = [
            ("open_positions",    "direction",      "ALTER TABLE open_positions ADD COLUMN direction TEXT DEFAULT 'BUY'"),
            ("open_positions",    "sl",             "ALTER TABLE open_positions ADD COLUMN sl REAL DEFAULT 0.0"),
            ("open_positions",    "tp",             "ALTER TABLE open_positions ADD COLUMN tp REAL DEFAULT 0.0"),
            ("open_positions",    "close_reason",   "ALTER TABLE open_positions ADD COLUMN close_reason TEXT"),
            ("copytrade_history", "close_reason",   "ALTER TABLE copytrade_history ADD COLUMN close_reason TEXT DEFAULT ''"),
        ]
        for table, col_name, sql in migrations:
            try:
                self.db.execute(sql)
                self.db.commit()
                logger.info(f"Migração: coluna '{col_name}' adicionada a {table}.")
            except sqlite3.OperationalError:
                pass  # Coluna já existe

        # Índice em timestamp acelera get_today_stats e ORDER BY DESC do
        # get_trade_history quando a tabela cresce.
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_copytrade_history_timestamp "
            "ON copytrade_history(timestamp)"
        )
        self.db.commit()

        logger.info("Banco de dados SQLite inicializado (4 tabelas).")

    def validate_broker_for_copytrade(self, broker_key: str) -> tuple[bool, str]:
        """
        Valida se um broker pode usar CopyTrade.
        Retorna (sucesso: bool, mensagem: str)

        Como o app só suporta NETTING, a validação sempre passa — mantida apenas
        como ponto de extensão para futuras checagens (ex.: trade enabled,
        margin level, etc.).
        """
        return True, "OK"

    # ──────────────────────────────────────────────
    # Bloco 1.5 - Gerenciamento de Status de Slaves
    # ──────────────────────────────────────────────
    def get_slave_status(self, slave_key: str) -> dict:
        """Retorna status do slave (ACTIVE/PAUSED)."""
        cursor = self.db.execute(
            "SELECT status, paused_reason, paused_at FROM slave_status WHERE slave_broker = ?",
            (slave_key,)
        )
        row = cursor.fetchone()
        if row:
            return {
                "status": row[0],
                "paused_reason": row[1],
                "paused_at": row[2]
            }
        return {"status": "ACTIVE", "paused_reason": None, "paused_at": None}

    def is_slave_paused(self, slave_key: str) -> bool:
        """Verifica se slave está pausado."""
        status = self.get_slave_status(slave_key)
        return status["status"] == "PAUSED"

    def pause_slave(self, slave_key: str, reason: str, ticket: int = None):
        """
        Pausa copytrader para um slave específico.
        reason: ex. "ALIEN_OPERATION", "MANUAL"
        """
        now = time.time()
        self.db.execute(
            """INSERT OR REPLACE INTO slave_status
               (slave_broker, status, paused_reason, paused_at, paused_by_ticket)
               VALUES (?, ?, ?, ?, ?)""",
            (slave_key, "PAUSED", reason, now, ticket)
        )
        self.db.commit()
        logger.warning(f"⏸️ CopyTrade PAUSADO para {slave_key}: {reason}")

    def resume_slave(self, slave_key: str):
        """Reativa copytrader para um slave."""
        self.db.execute(
            """UPDATE slave_status SET status = ?, paused_reason = ?, paused_at = NULL
               WHERE slave_broker = ?""",
            ("ACTIVE", None, slave_key)
        )
        self.db.commit()
        logger.info(f"▶️ CopyTrade REATIVADO para {slave_key}")

    # ──────────────────────────────────────────────
    # Bloco 2 - Cálculo de Lotes (risk-conservative)
    # ──────────────────────────────────────────────
    # Princípio: o risco relativo do slave nunca deve exceder o do master.
    # Sempre arredondamos para BAIXO ao step do símbolo. Se o resultado fica
    # abaixo do volume_min, o slave não entra / fecha tudo.
    #
    # Volumes vindos de aritmética float (multiplier * volume, ratio * volume)
    # podem ter ruído tipo 0.015000000000000002. O floor_to_step usa epsilon
    # pequeno para evitar que um valor "grudado" na borda caia um step a menos.

    @staticmethod
    def _floor_to_step(volume: float, step: float) -> float:
        """Arredonda volume para BAIXO ao múltiplo de step mais próximo."""
        if step <= 0:
            step = 0.01
        steps = math.floor(volume / step + 1e-9)
        return round(steps * step, 8)

    def calculate_slave_lot(self, master_lot: float, multiplier: float,
                            specs: dict = None) -> float:
        """
        Calcula lote do slave arredondando para BAIXO ao volume_step do símbolo.
        Retorna 0.0 se o resultado ficar abaixo de volume_min — chamador deve
        tratar como "slave não entra".
        """
        raw = master_lot * multiplier
        if not specs:
            # Fallback: step 0.01, min 0.01 (padrão FX)
            lot = self._floor_to_step(raw, 0.01)
            return lot if lot >= 0.01 else 0.0

        step = specs.get("volume_step", 0.01) or 0.01
        vol_min = specs.get("volume_min", 0.01)
        vol_max = specs.get("volume_max", 100.0)

        lot = self._floor_to_step(raw, step)
        if lot < vol_min:
            return 0.0
        if lot > vol_max:
            lot = self._floor_to_step(vol_max, step)
        return lot

    async def _fetch_symbol_specs(self, broker_key: str, symbol: str) -> dict:
        """
        Busca VOLUME_MIN, VOLUME_MAX, VOLUME_STEP do símbolo via EA.
        Cacheia resultado para não repetir.
        Retorna dict com specs ou None se falhar.
        """
        cache_key = (broker_key, symbol)
        if cache_key in self.symbol_specs_cache:
            return self.symbol_specs_cache[cache_key]

        try:
            request_id = f"symbol_info_{broker_key}_{symbol}_{time.time_ns()}"
            response = await self.tcp_router.send_command_to_broker(
                broker_key, "GET_SYMBOL_INFO", {"symbol": symbol}, request_id
            )

            if response.get("status") == "OK":
                specs = {
                    "volume_min": response.get("volume_min", 0.01),
                    "volume_max": response.get("volume_max", 100.0),
                    "volume_step": response.get("volume_step", 0.01),
                }
                self.symbol_specs_cache[cache_key] = specs
                logger.info(f"  📐 Symbol specs {symbol}@{broker_key}: min={specs['volume_min']}, max={specs['volume_max']}, step={specs['volume_step']}")
                return specs
            else:
                logger.warning(f"  ⚠️ Falha ao buscar specs de {symbol}@{broker_key}: {response.get('message', '?')}")
                return None
        except Exception as e:
            logger.error(f"  Erro ao buscar symbol specs de {symbol}@{broker_key}: {e}")
            return None

    def normalize_volume(self, volume: float, specs: dict) -> float:
        """
        Normaliza volume para abertura/ADD: floor ao step, respeitando min/max.
        Retorna 0.0 se ficar abaixo de volume_min — chamador trata como "não abre".
        """
        if not specs:
            specs = {}
        step = specs.get("volume_step", 0.01) or 0.01
        vol_min = specs.get("volume_min", 0.01)
        vol_max = specs.get("volume_max", 100.0)

        normalized = self._floor_to_step(volume, step)
        if normalized < vol_min:
            logger.warning(f"    ⚠️ Volume {volume} → floor {normalized} < mínimo {vol_min}. Operação cancelada.")
            return 0.0
        if normalized > vol_max:
            normalized = self._floor_to_step(vol_max, step)
            logger.warning(f"    ⚠️ Volume {volume} excede máximo. Limitado a {normalized}")
        return normalized

    def calculate_close_volume(self, master_remaining: float, master_before: float,
                                slave_current: float, specs: dict) -> tuple[float, bool]:
        """
        Calcula quanto do slave deve ser fechado em uma redução parcial do master,
        garantindo que o risco relativo do slave fique <= ao do master.

        Retorna (close_volume, is_full_close).
        - Calcula o volume IDEAL que deve SOBRAR no slave (floor ao step).
        - Se esse ideal ficar abaixo de volume_min, fecha TUDO.
        - Senão, fecha a diferença (slave_current - slave_remaining_ideal).

        Exemplo: master 0.10 fechando 0.09 (sobra 0.01 = 10%), slave tem 0.05,
        step=0.01, min=0.01. slave_ideal = floor(0.05 * 0.10/0.10) = 0.005 < 0.01
        → fecha tudo (0.05).
        """
        step = specs.get("volume_step", 0.01) if specs else 0.01
        vol_min = specs.get("volume_min", 0.01) if specs else 0.01

        if master_before <= 0 or slave_current <= 0:
            return slave_current, True

        ratio = max(master_remaining, 0.0) / master_before
        slave_remaining_ideal = self._floor_to_step(slave_current * ratio, step)

        if slave_remaining_ideal < vol_min:
            return slave_current, True

        close_volume = round(slave_current - slave_remaining_ideal, 8)
        close_volume = self._floor_to_step(close_volume, step)
        if close_volume < vol_min:
            # Redução tão pequena que não cabe no step — fecha tudo para não
            # deixar slave mais arriscado (alternativa seria ignorar, mas aí
            # o risco relativo cresce)
            return slave_current, True
        return close_volume, False

    # ──────────────────────────────────────────────
    # Bloco 3 - Processamento de Trade Events do Master
    # ──────────────────────────────────────────────
    # CHAVE DE DESIGN: Toda posição é rastreada pelo POSITION_IDENTIFIER do MQL5.
    # Este ID é imutável — conecta abertura, parciais e fechamento total.
    # O campo "master_ticket" no DB open_positions armazena o POSITION_IDENTIFIER (NÃO o deal).
    # O campo "master_ticket" no DB copytrade_history armazena o deal (log/auditoria).

    def _get_position_lock(self, position_id: int) -> asyncio.Lock:
        """Retorna (ou cria) um asyncio.Lock para o position_id dado.
        Garante que eventos para o mesmo position_id sejam processados em ordem
        (ex: BUY antes de CLOSE), evitando race condition."""
        if position_id not in self._position_locks:
            self._position_locks[position_id] = asyncio.Lock()
        return self._position_locks[position_id]

    def _cleanup_position_lock(self, position_id: int):
        """Remove lock de position_id quando não há mais posições abertas para ele."""
        lock = self._position_locks.get(position_id)
        if lock and not lock.locked():
            self._position_locks.pop(position_id, None)

    async def handle_master_trade_event(self, trade_event: dict):
        """
        Recebe TRADE_EVENT do Master EA e replica para todos os Slaves conectados.
        Validações rápidas são feitas fora do lock. A replicação é serializada
        por position_id para garantir ordem (BUY antes de CLOSE).
        """
        master_broker = trade_event.get("broker_key")
        request_data = trade_event.get("request", {})
        result_data = trade_event.get("result", {})

        # Suprimir replicação durante emergency close (evita double close)
        if self._emergency_active:
            logger.warning(f"  Replicação suprimida: emergency close ativo")
            return

        # Grace period: suprimir TRADE_EVENTs que chegam logo após emergency
        # (o master envia TRADE_EVENT async, pode chegar após _emergency_active=False)
        if self._emergency_completed_at > 0:
            elapsed = time.time() - self._emergency_completed_at
            if elapsed < 5.0:
                logger.warning(f"  Replicação suprimida: grace period pós-emergency ({elapsed:.1f}s < 5s)")
                return
            else:
                self._emergency_completed_at = 0  # Grace period expirado

        # Verifica se é realmente do master
        broker_role = self.broker_manager.get_broker_role(master_broker)
        if broker_role != "master":
            return

        # Filtrar ações não replicáveis antes de extrair tudo (reduz barulho no log)
        action = request_data.get("action", 0)
        if action != 1:  # Só TRADE_ACTION_DEAL (1) é replicável
            return

        # Extrair informações do trade
        symbol = request_data.get("symbol", "")
        volume = request_data.get("volume", 0)
        sl = request_data.get("sl", 0)
        tp = request_data.get("tp", 0)
        order_type = request_data.get("type", 0)
        position_ticket = request_data.get("position", 0)
        retcode = result_data.get("retcode", 0)
        deal_ticket = result_data.get("deal", 0) or result_data.get("order", 0)
        position_volume_remaining = trade_event.get("position_volume_remaining")

        # POSITION_IDENTIFIER — chave universal (vem do EA)
        position_id = trade_event.get("position_id", 0)

        # Só replica trades com sucesso (retcode 10009 = TRADE_RETCODE_DONE)
        if retcode != 10009 and retcode != 0:
            logger.warning(f"Trade event com retcode={retcode}, não será replicado.")
            return

        if not symbol:
            logger.warning("Trade event sem símbolo, ignorando.")
            return

        # Reversal sintético do EA (OnTrade detectou cruzamento de zero em netting).
        # O evento carrega new_direction/new_volume já calculados — dispensa inferir
        # do DB e é imune ao bug em que o master_volume_current ficava dessincronizado
        # após uma ordem oposta de volume maior que a posição.
        is_reversal_event = bool(trade_event.get("is_reversal"))
        new_direction = trade_event.get("new_direction")
        new_volume = trade_event.get("new_volume")

        if is_reversal_event:
            trade_action = "REVERSAL"
            logger.info(f"  🔄 REVERSAL sintético: {trade_event.get('old_direction')} {trade_event.get('old_volume')} "
                        f"-> {new_direction} {new_volume}")
        else:
            # Determinar tipo de ação para replicação
            trade_action = self._classify_trade_action(action, order_type, position_ticket, position_volume_remaining)
            if not trade_action:
                return

        # Validar que temos position_id (obrigatório para tracking)
        if not position_id:
            # OnTradeTransaction pode emitir um TRADE_EVENT secundário do deal
            # de fechamento sem position_id (request.position vem 0 quando o
            # broker processa o close de forma assíncrona, ex.: B3). O snapshot
            # diff do OnTrade já capturou o mesmo fechamento com position_id
            # válido — silencia o eco em vez de logar como erro.
            logger.debug(
                f"  TRADE_EVENT sem position_id (provável eco pós-fechamento). deal={deal_ticket}"
            )
            return

        # Dedup: OnTrade() e OnTradeTransaction() podem emitir para o mesmo trade.
        # order_type no key evita falso positivo se o trader reverter no mesmo segundo
        # (ex: BUY 10 seguido de SELL 10 — order_type 0 vs 1).
        timestamp_mql = trade_event.get("timestamp_mql", 0)
        dedup_key = (position_id, timestamp_mql, order_type)
        now = time.time()
        if dedup_key in self._master_event_dedup:
            return
        self._master_event_dedup[dedup_key] = now
        # No reversal sintético, o OnTradeTransaction subsequente chega com o volume
        # total da ordem (ex: BUY 0.14 fechando SELL 0.10 para abrir BUY 0.04) e
        # seria reprocessado como BUY novo. Registrar ambos order_types no dedup
        # impede esse reprocessamento — o evento sintético é a fonte de verdade.
        if is_reversal_event:
            opposite = 0 if order_type == 1 else 1
            self._master_event_dedup[(position_id, timestamp_mql, opposite)] = now
        # Evict expired entries only when dict grows large (amortized O(1)).
        if len(self._master_event_dedup) > 200:
            self._master_event_dedup = {k: v for k, v in self._master_event_dedup.items() if now - v < 10}

        # ── Serializar por position_id ──
        # Garante que BUY completa antes de CLOSE para o mesmo position_id.
        # Eventos de position_ids diferentes rodam em paralelo (tasks separadas).
        lock = self._get_position_lock(position_id)
        async with lock:
            log_msg = f"MASTER [{master_broker}]: {trade_action} {symbol} {volume} lotes (pos_id={position_id}, deal={deal_ticket})"
            self.copy_trade_log.emit(log_msg)
            logger.info(log_msg)

            # Ler estado atual do master ANTES de atualizar (prev_vol para cálculos do slave)
            master_info_before = self._get_master_position_info(position_id, master_broker)

            # Atualizar master_positions (fonte de verdade do estado do master — todos os actions)
            direction_str = "BUY" if order_type == 0 else "SELL"
            if is_reversal_event:
                self._track_master_position(
                    position_id, float(new_volume or 0), "REVERSAL",
                    master_broker=master_broker, symbol=symbol,
                    direction=new_direction or direction_str, sl=sl, tp=tp
                )
            else:
                self._track_master_position(
                    position_id, volume, trade_action,
                    master_broker=master_broker, symbol=symbol,
                    direction=direction_str, sl=sl, tp=tp
                )

            # Replica para cada slave conectado (em paralelo entre slaves)
            slaves = self.broker_manager.get_connected_slave_brokers()
            tasks = []
            for slave_key in slaves:
                if self.is_slave_paused(slave_key):
                    logger.warning(f"  Pulando {slave_key} (pausado)")
                    continue

                tasks.append(self._replicate_to_slave(
                    slave_key, master_broker, deal_ticket, position_id,
                    trade_action, symbol, volume, order_type, sl, tp,
                    reversal_new_direction=new_direction if is_reversal_event else None,
                    reversal_new_volume=new_volume if is_reversal_event else None,
                    master_info_before=master_info_before,
                ))

            if tasks:
                await asyncio.gather(*tasks)

        # Limpar lock se posição foi fechada (não há mais eventos esperados)
        if trade_action == "CLOSE":
            self._cleanup_position_lock(position_id)

    async def handle_master_sltp_update(self, sltp_data: dict):
        """Replica modificação de SL/TP do Master para todos os Slaves."""
        position_id = sltp_data.get("position_id", 0)
        symbol = sltp_data.get("symbol", "")
        new_sl = sltp_data.get("sl", 0.0)
        new_tp = sltp_data.get("tp", 0.0)

        if not position_id:
            logger.warning("SLTP_MODIFIED sem position_id, ignorando")
            return

        logger.info(f"SLTP_MODIFIED: pos_id={position_id}, symbol={symbol}, sl={new_sl}, tp={new_tp}")

        with self.db:
            self.db.execute(
                "UPDATE open_positions SET sl = ?, tp = ? WHERE master_ticket = ? AND status = 'OPEN'",
                (new_sl, new_tp, position_id)
            )
            self.db.execute(
                "UPDATE master_positions SET sl = ?, tp = ? "
                "WHERE position_id = ? AND master_broker = ? AND status = 'OPEN'",
                (new_sl, new_tp, position_id, sltp_data.get("broker_key", ""))
            )

        slaves = self.broker_manager.get_connected_slave_brokers()
        tasks = []
        for slave_key in slaves:
            if self.is_slave_paused(slave_key):
                continue

            pos_info = self._get_slave_position_info(position_id, slave_key)
            if pos_info is None or pos_info["volume"] <= 0:
                continue

            slave_ticket = self._get_slave_ticket(position_id, slave_key)
            if not slave_ticket:
                logger.warning(f"  SLTP: slave {slave_key} sem ticket para pos_id={position_id}")
                continue

            tasks.append(self._replicate_sltp_to_slave(
                slave_key, slave_ticket, new_sl, new_tp, position_id, symbol
            ))

        if tasks:
            await asyncio.gather(*tasks)

    async def _replicate_sltp_to_slave(self, slave_key: str, slave_ticket: int,
                                       sl: float, tp: float, position_id: int, symbol: str):
        """Envia TRADE_POSITION_MODIFY para um slave."""
        logger.info(f"  SLTP -> {slave_key}: ticket={slave_ticket}, sl={sl}, tp={tp}")

        request_id = f"sltp_{slave_key}_{position_id}_{time.time_ns()}"
        response = await self.tcp_router.send_command_to_broker(
            slave_key, "TRADE_POSITION_MODIFY",
            {"ticket": slave_ticket, "sl": sl, "tp": tp},
            request_id
        )

        if response.get("status") == "OK":
            logger.info(f"  SLTP OK: {slave_key} pos_id={position_id}")
            self.copy_trade_log.emit(f"SLTP [{slave_key}]: {symbol} sl={sl} tp={tp}")
        else:
            error = response.get("error_message", "unknown")
            logger.error(f"  SLTP FALHOU: {slave_key} - {error}")
            self.copy_trade_log.emit(f"SLTP ERRO [{slave_key}]: {symbol} - {error}")

    def _classify_trade_action(self, action: int, order_type: int, position_ticket: int,
                               position_volume_remaining=None) -> str:
        """
        Classifica a ação do trade com base nos dados do MQL5.
        action: TRADE_ACTION_DEAL=1, TRADE_ACTION_PENDING=5, TRADE_ACTION_SLTP=6,
                TRADE_ACTION_MODIFY=7, TRADE_ACTION_REMOVE=8
        order_type: 0=BUY, 1=SELL, 2=BUY_LIMIT, 3=SELL_LIMIT, 4=BUY_STOP, 5=SELL_STOP
        position_volume_remaining: volume restante após fechamento (0 = total, >0 = parcial)
        """
        if action == 1:  # TRADE_ACTION_DEAL (market order)
            if position_ticket > 0:
                if position_volume_remaining is not None and position_volume_remaining > 0:
                    return "PARTIAL_CLOSE"
                return "CLOSE"
            if order_type == 0:
                return "BUY"
            elif order_type == 1:
                return "SELL"
        return None

    def _track_master_position(self, position_id: int, volume: float, trade_action: str,
                                master_broker: str = "", symbol: str = "",
                                direction: str = "", sl: float = 0.0, tp: float = 0.0):
        """
        Mantém master_positions como fonte de verdade do estado do master.
        Cobre todos os trade_actions: BUY/SELL (open ou ADD), REVERSAL, PARTIAL_CLOSE, CLOSE.
        """
        now = time.time()

        with self.db:
            if trade_action in ("BUY", "SELL"):
                existing = self._get_master_position_info(position_id, master_broker)
                if existing:
                    # ADD à posição existente
                    self.db.execute(
                        "UPDATE master_positions SET volume = ROUND(volume + ?, 8), sl = ?, tp = ? "
                        "WHERE position_id = ? AND master_broker = ? AND status = 'OPEN'",
                        (volume, sl, tp, position_id, master_broker)
                    )
                else:
                    self.db.execute(
                        """INSERT INTO master_positions
                           (master_broker, position_id, symbol, direction, volume, volume_original,
                            sl, tp, status, opened_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)""",
                        (master_broker, position_id, symbol, direction, volume, volume, sl, tp, now)
                    )

            elif trade_action == "REVERSAL":
                self.db.execute(
                    "UPDATE master_positions SET direction = ?, volume = ROUND(?, 8), sl = ?, tp = ? "
                    "WHERE position_id = ? AND master_broker = ? AND status = 'OPEN'",
                    (direction, volume, sl, tp, position_id, master_broker)
                )

            elif trade_action == "PARTIAL_CLOSE":
                self.db.execute(
                    "UPDATE master_positions SET volume = ROUND(MAX(0, volume - ?), 8) "
                    "WHERE position_id = ? AND master_broker = ? AND status = 'OPEN'",
                    (volume, position_id, master_broker)
                )
                # open_positions legacy: mantido para compatibilidade
                self.db.execute(
                    "UPDATE open_positions SET master_volume_current = master_volume_current - ? "
                    "WHERE master_ticket = ? AND status = 'OPEN'",
                    (volume, position_id)
                )

            elif trade_action == "CLOSE":
                self.db.execute(
                    "UPDATE master_positions SET status = 'CLOSED', closed_at = ? "
                    "WHERE position_id = ? AND master_broker = ? AND status = 'OPEN'",
                    (now, position_id, master_broker)
                )
                self.db.execute(
                    "UPDATE open_positions SET status = 'CLOSING' WHERE master_ticket = ? AND status = 'OPEN'",
                    (position_id,)
                )

    def _get_slave_position_info(self, position_id: int, slave_key: str) -> dict:
        """Retorna info da posição do slave: {volume, direction, master_volume}. None se não encontrado."""
        row = self.db.execute(
            """SELECT slave_volume_current, direction, master_volume_current
               FROM open_positions
               WHERE master_ticket = ? AND slave_broker = ? AND status IN ('OPEN', 'CLOSING')""",
            (position_id, slave_key)
        ).fetchone()
        if row:
            return {
                "volume": round(row[0], 8),
                "direction": row[1] or "BUY",
                "master_volume": round(row[2], 8) if row[2] is not None else 0.0,
            }
        return None

    def _get_master_position_info(self, position_id: int, master_broker: str) -> dict | None:
        """Retorna estado atual do master: {volume, direction, sl, tp}. None se não encontrado."""
        row = self.db.execute(
            "SELECT volume, direction, sl, tp FROM master_positions "
            "WHERE position_id = ? AND master_broker = ? AND status = 'OPEN'",
            (position_id, master_broker)
        ).fetchone()
        if row:
            return {
                "volume": round(row[0], 8),
                "direction": row[1],
                "sl": row[2] or 0.0,
                "tp": row[3] or 0.0,
            }
        return None

    def _get_slave_ticket(self, position_id: int, slave_key: str):
        """Busca slave_ticket pelo position_id — primeiro em memória, depois no DB."""
        # 1. Cache em memória (rápido)
        ticket = self.position_map.get(position_id, {}).get(slave_key)
        if ticket:
            return ticket
        # 2. Fallback: DB (persistente — sobrevive a restarts do Python)
        row = self.db.execute(
            "SELECT slave_ticket FROM open_positions WHERE master_ticket = ? AND slave_broker = ? AND status IN ('OPEN', 'CLOSING')",
            (position_id, slave_key)
        ).fetchone()
        return row[0] if row else None

    async def _replicate_to_slave(self, slave_key: str, master_broker: str,
                                   deal_ticket: int, position_id: int,
                                   trade_action: str, symbol: str, volume: float,
                                   order_type: int, sl: float, tp: float,
                                   reversal_new_direction: str = None,
                                   reversal_new_volume: float = None,
                                   master_info_before: dict = None):
        """
        Envia comando de trade para um slave específico (NETTING mode).

        Princípios:
          - Risco do slave NUNCA excede o do master (arredondamento conservador).
          - Redução proporcional: slave mantém no máximo (master_remaining/master_before)
            × slave_current, com floor ao volume_step. Se o restante ideal < volume_min,
            fecha tudo.
          - Reversal: se master inverte direção em volume > master_prev_vol, slave
            fecha existente e abre nova na direção oposta com floor do excess.

        Comandos do EA usados (NETTING):
          - TRADE_ORDER_TYPE_BUY  → abre/adiciona long ou reduz short
          - TRADE_ORDER_TYPE_SELL → abre/adiciona short ou reduz long
          - TRADE_POSITION_CLOSE_ID → fecha posição inteira por ticket
        """
        symbol_specs = await self._fetch_symbol_specs(slave_key, symbol)

        # Se o GET_SYMBOL_INFO falhou, o símbolo não existe nesse broker
        # (ex.: forex no slave B3, ou contrato vencido). Pula sem tentar enviar
        # — o trade falharia com "Símbolo X não disponível" e geraria 3 logs.
        if symbol_specs is None:
            logger.info(f"    ⏭️  SKIP {slave_key}: {symbol} não disponível no broker")
            self._insert_history(master_broker, deal_ticket, symbol, trade_action,
                                 volume, slave_key, 0, 0, "SKIPPED",
                                 "símbolo não disponível no broker")
            self.copy_trade_log.emit(f"SKIP [{slave_key}]: {symbol} indisponível")
            return

        multiplier = self.broker_manager.get_lot_multiplier(slave_key)

        pos_info = self._get_slave_position_info(position_id, slave_key)
        has_open_position = pos_info is not None and pos_info["volume"] > 0
        existing_slave_vol = pos_info["volume"] if pos_info else 0.0
        existing_direction = pos_info["direction"] if pos_info else None
        # master_prev_vol: estado do master ANTES do evento atual (master_positions é a fonte)
        master_prev_vol = master_info_before["volume"] if master_info_before else 0.0

        # ── REVERSAL sintético (EA detectou cruzamento de zero via OnTrade) ──
        # O EA já entregou new_direction/new_volume — não dependemos do DB
        # pro master_prev_vol, que estaria desatualizado após o cruzamento.
        if trade_action == "REVERSAL":
            new_vol = float(reversal_new_volume or 0.0)
            new_dir = reversal_new_direction or ("BUY" if order_type == 0 else "SELL")
            if has_open_position:
                await self._execute_reversal(
                    slave_key, master_broker, deal_ticket, position_id, symbol,
                    new_vol, new_dir, new_vol, multiplier,
                    existing_slave_vol, symbol_specs, sl, tp
                )
                return
            # Slave sem posição (floor anterior zerou ou slave nunca entrou):
            # abre direto na direção nova com o volume da perna nova.
            slave_lot = self.calculate_slave_lot(new_vol, multiplier, symbol_specs)
            if slave_lot <= 0:
                logger.info(f"    🔄 REVERSAL sem posição: {new_vol} × {multiplier} < volume_min. Slave fica zerado.")
                self._insert_history(master_broker, deal_ticket, symbol, f"REVERSAL_{new_dir}",
                                     new_vol, slave_key, 0, 0, "SKIPPED",
                                     "excess < volume_min (slave sem posição prévia)")
                return
            logger.info(f"    🔄 REVERSAL sem posição: abrindo {new_dir} {slave_lot} direto")
            await self._send_open_command(
                slave_key, master_broker, deal_ticket, position_id, symbol,
                new_vol, slave_lot, new_dir, sl, tp
            )
            return

        # ── CLOSE TOTAL ──
        if trade_action == "CLOSE":
            await self._replicate_close(slave_key, master_broker, deal_ticket, position_id,
                                         symbol, volume, has_open_position, existing_slave_vol)
            return

        # ── PARTIAL_CLOSE (classificado pelo EA via position_volume_remaining) ──
        if trade_action == "PARTIAL_CLOSE":
            if not has_open_position:
                logger.warning(f"    ⚠️ PARTIAL_CLOSE ignorado: slave sem posição aberta (pos_id={position_id})")
                self._insert_history(master_broker, deal_ticket, symbol, trade_action,
                                     volume, slave_key, 0, 0, "SKIPPED", "slave sem posição aberta")
                return

            # master_prev_vol é o volume ANTES do partial close (de master_info_before).
            # O `volume` do evento é quanto o master fechou agora.
            master_before = master_prev_vol
            master_remaining = max(0.0, master_before - volume)

            close_vol, is_full_close = self.calculate_close_volume(
                master_remaining, master_before, existing_slave_vol, symbol_specs
            )
            logger.info(f"    PARTIAL_CLOSE: master {master_before}→{master_remaining}, "
                        f"slave {existing_slave_vol} → fecha {close_vol} "
                        f"({'total' if is_full_close else 'parcial'})")

            # Direção do comando: oposta da posição do slave
            close_direction = "BUY" if existing_direction == "SELL" else "SELL"
            await self._send_reduce_command(
                slave_key, master_broker, deal_ticket, position_id, symbol,
                volume, close_vol, close_direction, existing_slave_vol, is_full_close, "PARTIAL_CLOSE"
            )
            return

        # ── BUY/SELL (abertura, ADD, REDUCE ou REVERSAL em NETTING) ──
        if trade_action not in ("BUY", "SELL"):
            logger.warning(f"Ação não suportada: {trade_action}")
            return

        if not has_open_position:
            # Abertura nova (floor ao step via calculate_slave_lot)
            slave_lot = self.calculate_slave_lot(volume, multiplier, symbol_specs)
            if slave_lot <= 0:
                logger.warning(f"    ❌ Slave não entra: {volume} × {multiplier} abaixo do volume_min")
                self._insert_history(master_broker, deal_ticket, symbol, trade_action,
                                     volume, slave_key, 0, 0, "SKIPPED", "volume < mínimo do símbolo")
                return
            await self._send_open_command(
                slave_key, master_broker, deal_ticket, position_id, symbol,
                volume, slave_lot, trade_action, sl, tp
            )
            return

        # Slave já tem posição — decidir ADD, REDUCE ou REVERSAL
        if trade_action == existing_direction:
            # ADD: mesma direção → aumentar
            slave_lot = self.calculate_slave_lot(volume, multiplier, symbol_specs)
            if slave_lot <= 0:
                logger.warning(f"    ⚠️ ADD ignorado: volume {volume} × {multiplier} abaixo do volume_min")
                self._insert_history(master_broker, deal_ticket, symbol, trade_action,
                                     volume, slave_key, 0, 0, "SKIPPED", "volume < mínimo do símbolo")
                return
            logger.info(f"    📊 ADD: slave {existing_slave_vol} {existing_direction} += {slave_lot} (master +{volume})")
            await self._send_add_command(
                slave_key, master_broker, deal_ticket, position_id, symbol,
                volume, slave_lot, trade_action, existing_slave_vol, sl, tp
            )
            return

        # Direção oposta: é REVERSAL se volume > master_prev_vol, senão REDUCE puro
        is_reversal = volume > master_prev_vol + 1e-9 and master_prev_vol > 0

        if is_reversal:
            master_excess = round(volume - master_prev_vol, 8)
            logger.info(f"    🔄 REVERSAL detectado: master {master_prev_vol} {existing_direction}"
                        f" → {volume} {trade_action} (excess={master_excess})")
            await self._execute_reversal(
                slave_key, master_broker, deal_ticket, position_id, symbol,
                volume, trade_action, master_excess, multiplier,
                existing_slave_vol, symbol_specs, sl, tp
            )
            return

        # REDUCE puro (master está parcialmente fechando via ordem oposta sem inverter)
        master_remaining = master_prev_vol - volume
        if master_remaining < 0:
            master_remaining = 0.0
        close_vol, is_full_close = self.calculate_close_volume(
            master_prev_vol - volume, master_prev_vol, existing_slave_vol, symbol_specs
        )
        logger.info(f"    📊 REDUCE: master {master_prev_vol}→{master_remaining}, "
                    f"slave {existing_slave_vol} → fecha {close_vol} "
                    f"({'total' if is_full_close else 'parcial'})")
        await self._send_reduce_command(
            slave_key, master_broker, deal_ticket, position_id, symbol,
            volume, close_vol, trade_action, existing_slave_vol, is_full_close, "REDUCE"
        )

    # ── Helpers de execução (sub-operações de _replicate_to_slave) ──

    async def _replicate_close(self, slave_key, master_broker, deal_ticket, position_id,
                                symbol, volume, has_open_position, existing_slave_vol):
        slave_ticket = self._get_slave_ticket(position_id, slave_key)
        if not slave_ticket or not has_open_position:
            reason = "slave sem posição aberta" if not has_open_position else "sem mapeamento ticket"
            logger.warning(f"    ⚠️ CLOSE ignorado: {reason} (pos_id={position_id})")
            self._insert_history(master_broker, deal_ticket, symbol, "CLOSE",
                                 volume, slave_key, 0, 0, "SKIPPED", reason)
            return

        record_id = self._insert_history(master_broker, deal_ticket, symbol, "CLOSE",
                                          volume, slave_key, 0, existing_slave_vol, "PENDING")
        log_msg = f"COPY [{slave_key}]: CLOSE {symbol} {existing_slave_vol} lotes"
        self.copy_trade_log.emit(log_msg)
        logger.info(f"    {log_msg} → TRADE_POSITION_CLOSE_ID (ticket={slave_ticket})")

        request_id = f"trade_{slave_key}_{time.time_ns()}"
        response = await self.tcp_router.send_command_to_broker(
            slave_key, "TRADE_POSITION_CLOSE_ID", {"ticket": slave_ticket}, request_id
        )

        if response.get("status") == "OK":
            slave_result_ticket = response.get("order", 0) or response.get("deal", 0)
            self._update_history(record_id, "SUCCESS", slave_result_ticket, close_reason="COPYTRADE")
            self._on_close_success(position_id, slave_key)
            self.copy_trade_executed.emit({"slave": slave_key, "symbol": symbol,
                                            "action": "CLOSE", "lot": existing_slave_vol})
            return

        error = response.get("error_message", "") or response.get("message", "Erro desconhecido")
        if "não encontrada" in error.lower():
            resolved = await self._verify_position_closed(slave_key, symbol, position_id)
            if resolved:
                self._update_history(record_id, "SUCCESS", 0,
                                     "Posição já fechada pelo broker (SL/TP/SO)",
                                     close_reason="BROKER_SLTP")
                return

        self._update_history(record_id, "FAILED", 0, error)
        self.copy_trade_failed.emit({"slave": slave_key, "symbol": symbol,
                                      "action": "CLOSE", "error": error})
        logger.error(f"Falha ao replicar CLOSE para {slave_key}: {error}")

    async def _send_reduce_command(self, slave_key, master_broker, deal_ticket, position_id,
                                    symbol, master_volume, close_vol, close_direction,
                                    existing_slave_vol, is_full_close, action_label):
        """Envia redução (PARTIAL_CLOSE ou REDUCE). Se is_full_close, usa CLOSE_ID;
        senão envia ordem oposta com volume parcial."""
        if close_vol <= 0:
            logger.warning(f"    ⚠️ {action_label} sem efeito: close_vol=0")
            self._insert_history(master_broker, deal_ticket, symbol, action_label,
                                 master_volume, slave_key, 0, 0, "SKIPPED", "volume=0")
            return

        if is_full_close:
            slave_ticket = self._get_slave_ticket(position_id, slave_key)
            if not slave_ticket:
                logger.warning(f"    ⚠️ {action_label}: sem slave_ticket, pulando")
                self._insert_history(master_broker, deal_ticket, symbol, action_label,
                                     master_volume, slave_key, 0, 0, "SKIPPED", "sem ticket")
                return
            command = "TRADE_POSITION_CLOSE_ID"
            payload = {"ticket": slave_ticket}
        else:
            command = f"TRADE_ORDER_TYPE_{close_direction}"
            payload = {"symbol": symbol, "volume": float(close_vol),
                       "price": 0.0, "sl": 0.0, "tp": 0.0,
                       "deviation": 10, "comment": f"CT:{position_id}"}

        record_id = self._insert_history(master_broker, deal_ticket, symbol, action_label,
                                          master_volume, slave_key, 0, close_vol, "PENDING")
        log_msg = f"COPY [{slave_key}]: {action_label} {symbol} {close_vol} lotes"
        self.copy_trade_log.emit(log_msg)
        logger.info(f"    {log_msg} → {command}")

        request_id = f"trade_{slave_key}_{time.time_ns()}"
        response = await self.tcp_router.send_command_to_broker(
            slave_key, command, payload, request_id
        )

        if response.get("status") == "OK":
            slave_result_ticket = response.get("order", 0) or response.get("deal", 0)
            self._update_history(record_id, "SUCCESS", slave_result_ticket)
            if is_full_close:
                self._on_close_success(position_id, slave_key)
            else:
                closed_vol = response.get("volume", close_vol)
                self._on_partial_close_success(position_id, slave_key, closed_vol)
            self.copy_trade_executed.emit({"slave": slave_key, "symbol": symbol,
                                            "action": action_label, "lot": close_vol})
            return

        error = response.get("error_message", "") or response.get("message", "Erro desconhecido")

        # Caso B3: master e slaves compartilham cotação do mesmo símbolo, então
        # o slave pode ter fechado por TP/SL local quase no mesmo instante que
        # o master — quando o PARTIAL_CLOSE chega aqui, a posição já não existe.
        # Mesmo fallback usado em _replicate_close: confirma via GET_POSITIONS
        # antes de marcar FAILED. Só vale quando is_full_close (usa CLOSE_ID por
        # ticket); partial real envia ordem oposta nova, não tem esse caso.
        if is_full_close and "não encontrada" in error.lower():
            resolved = await self._verify_position_closed(slave_key, symbol, position_id)
            if resolved:
                self._update_history(record_id, "SUCCESS", 0,
                                     "Posição já fechada pelo broker (SL/TP/SO)",
                                     close_reason="BROKER_SLTP")
                return

        self._update_history(record_id, "FAILED", 0, error)
        self.copy_trade_failed.emit({"slave": slave_key, "symbol": symbol,
                                      "action": action_label, "error": error})
        logger.error(f"Falha ao replicar {action_label} para {slave_key}: {error}")

    async def _send_open_command(self, slave_key, master_broker, deal_ticket, position_id,
                                  symbol, master_volume, slave_lot, direction,
                                  sl, tp):
        command = f"TRADE_ORDER_TYPE_{direction}"
        # price=0.0: o slave é outra corretora, com cotação própria. O EA usa
        # o ask/bid local quando price<=0. Encaminhar o preço do master causa
        # rejeição por desvio quando as cotações divergem entre brokers.
        payload = {"symbol": symbol, "volume": float(slave_lot),
                   "price": 0.0, "sl": sl, "tp": tp,
                   "deviation": 10, "comment": f"CT:{position_id}"}
        record_id = self._insert_history(master_broker, deal_ticket, symbol, direction,
                                          master_volume, slave_key, 0, slave_lot, "PENDING")
        log_msg = f"COPY [{slave_key}]: {direction} {symbol} {slave_lot} lotes"
        self.copy_trade_log.emit(log_msg)
        logger.info(f"    {log_msg} → {command}")

        request_id = f"trade_{slave_key}_{time.time_ns()}"
        response = await self.tcp_router.send_command_to_broker(
            slave_key, command, payload, request_id
        )

        if response.get("status") == "OK":
            slave_result_ticket = response.get("order", 0) or response.get("deal", 0)
            self._update_history(record_id, "SUCCESS", slave_result_ticket)
            self._on_open_success(position_id, slave_key, slave_result_ticket,
                                   slave_lot, symbol, master_volume, direction=direction)
            self.copy_trade_executed.emit({"slave": slave_key, "symbol": symbol,
                                            "action": direction, "lot": slave_lot})
            return

        error = response.get("error_message", "") or response.get("message", "Erro desconhecido")
        self._update_history(record_id, "FAILED", 0, error)
        self.copy_trade_failed.emit({"slave": slave_key, "symbol": symbol,
                                      "action": direction, "error": error})
        logger.error(f"Falha ao abrir para {slave_key}: {error}")

    async def _send_add_command(self, slave_key, master_broker, deal_ticket, position_id,
                                 symbol, master_volume, slave_lot, direction,
                                 existing_slave_vol, sl, tp):
        command = f"TRADE_ORDER_TYPE_{direction}"
        # price=0.0: ver _send_open_command — o slave usa a cotação local.
        payload = {"symbol": symbol, "volume": float(slave_lot),
                   "price": 0.0, "sl": sl, "tp": tp,
                   "deviation": 10, "comment": f"CT:{position_id}"}
        record_id = self._insert_history(master_broker, deal_ticket, symbol, direction,
                                          master_volume, slave_key, 0, slave_lot, "PENDING")
        log_msg = f"COPY [{slave_key}]: ADD {direction} {symbol} {slave_lot} lotes"
        self.copy_trade_log.emit(log_msg)
        logger.info(f"    {log_msg} → {command}")

        request_id = f"trade_{slave_key}_{time.time_ns()}"
        response = await self.tcp_router.send_command_to_broker(
            slave_key, command, payload, request_id
        )

        if response.get("status") == "OK":
            slave_result_ticket = response.get("order", 0) or response.get("deal", 0)
            self._update_history(record_id, "SUCCESS", slave_result_ticket)
            added_vol = response.get("volume", slave_lot)
            self._on_add_success(position_id, slave_key, added_vol, master_volume)
            new_slave_vol = round(existing_slave_vol + added_vol, 8)
            logger.info(f"    📊 ADD ok: +{added_vol} (slave: {existing_slave_vol} → {new_slave_vol})")
            self.copy_trade_executed.emit({"slave": slave_key, "symbol": symbol,
                                            "action": direction, "lot": slave_lot})
            return

        error = response.get("error_message", "") or response.get("message", "Erro desconhecido")
        self._update_history(record_id, "FAILED", 0, error)
        self.copy_trade_failed.emit({"slave": slave_key, "symbol": symbol,
                                      "action": direction, "error": error})
        logger.error(f"Falha ao ADD para {slave_key}: {error}")

    async def _execute_reversal(self, slave_key, master_broker, deal_ticket, position_id,
                                 symbol, master_volume, new_direction, master_excess,
                                 multiplier, existing_slave_vol, symbol_specs, sl, tp):
        """
        Reversal em 2 etapas:
          1. Fecha posição existente do slave (TRADE_POSITION_CLOSE_ID)
          2. Se `master_excess × multiplier` >= volume_min (floor ao step):
             abre nova posição na direção oposta com o excess.

        Se step 1 falhar: marca FAILED e não abre a 2.
        Se step 1 ok mas step 2 falhar: marca PARTIAL_REVERSAL_FAILED
        (slave fica zerado enquanto master tem posição na direção nova).
        """
        # Etapa 1: fechar posição existente do slave
        slave_ticket = self._get_slave_ticket(position_id, slave_key)
        if not slave_ticket:
            logger.error(f"    ❌ REVERSAL: sem slave_ticket para pos_id={position_id}")
            self._insert_history(master_broker, deal_ticket, symbol, "REVERSAL",
                                 master_volume, slave_key, 0, 0, "FAILED",
                                 "sem ticket para fechar na etapa 1 do reversal")
            return

        close_record = self._insert_history(master_broker, deal_ticket, symbol, "REVERSAL_CLOSE",
                                             master_volume, slave_key, 0, existing_slave_vol, "PENDING")
        close_request_id = f"trade_{slave_key}_{time.time_ns()}"
        close_response = await self.tcp_router.send_command_to_broker(
            slave_key, "TRADE_POSITION_CLOSE_ID", {"ticket": slave_ticket}, close_request_id
        )

        if close_response.get("status") != "OK":
            error = close_response.get("error_message", "") or close_response.get("message", "?")
            self._update_history(close_record, "FAILED", 0, f"REVERSAL close: {error}")
            self.copy_trade_failed.emit({"slave": slave_key, "symbol": symbol,
                                          "action": "REVERSAL", "error": error})
            logger.error(f"    ❌ REVERSAL abortado (close falhou): {error}")
            return

        close_deal = close_response.get("order", 0) or close_response.get("deal", 0)
        self._update_history(close_record, "SUCCESS", close_deal, close_reason="REVERSAL")
        self._on_close_success(position_id, slave_key, close_reason="REVERSAL")

        # Etapa 2: abrir nova posição na direção oposta com o excess
        reverse_lot = self.calculate_slave_lot(master_excess, multiplier, symbol_specs)
        if reverse_lot <= 0:
            logger.info(f"    🔄 REVERSAL: excess {master_excess} × {multiplier} abaixo do min. "
                        f"Slave fica zerado (close-only).")
            self._insert_history(master_broker, deal_ticket, symbol, "REVERSAL_OPEN",
                                 master_excess, slave_key, 0, 0, "SKIPPED",
                                 "excess < volume_min (slave respeita multiplier)")
            return

        open_record = self._insert_history(master_broker, deal_ticket, symbol, f"REVERSAL_{new_direction}",
                                            master_excess, slave_key, 0, reverse_lot, "PENDING")
        open_request_id = f"trade_{slave_key}_{time.time_ns()}_rev"
        command = f"TRADE_ORDER_TYPE_{new_direction}"
        payload = {"symbol": symbol, "volume": float(reverse_lot),
                   "price": 0.0, "sl": sl, "tp": tp,
                   "deviation": 10, "comment": f"CT:{position_id}"}
        logger.info(f"    REVERSAL etapa 2: {command} {reverse_lot}")

        open_response = await self.tcp_router.send_command_to_broker(
            slave_key, command, payload, open_request_id
        )

        if open_response.get("status") == "OK":
            new_slave_ticket = open_response.get("order", 0) or open_response.get("deal", 0)
            new_slave_vol = open_response.get("volume", reverse_lot)
            self._update_history(open_record, "SUCCESS", new_slave_ticket)
            self._on_open_success(position_id, slave_key, new_slave_ticket,
                                   new_slave_vol, symbol, master_excess, direction=new_direction)
            logger.info(f"    🔄 REVERSAL ok: slave agora {new_slave_vol} {new_direction} "
                        f"(novo ticket {new_slave_ticket})")
            self.copy_trade_executed.emit({"slave": slave_key, "symbol": symbol,
                                            "action": f"REVERSAL_{new_direction}", "lot": reverse_lot})
            return

        # Step 2 falhou — slave já está zerado (step 1 ok)
        error = open_response.get("error_message", "") or open_response.get("message", "?")
        self._update_history(open_record, "PARTIAL_REVERSAL_FAILED", 0,
                             f"REVERSAL open falhou após close ok: {error}")
        self.copy_trade_failed.emit({"slave": slave_key, "symbol": symbol,
                                      "action": "REVERSAL", "error": f"open falhou: {error}"})
        logger.error(f"    ❌ PARTIAL_REVERSAL_FAILED: slave {slave_key} zerado, master tem posição nova. "
                     f"Intervenção manual pode ser necessária.")

    # ── Verificação pós-falha ──

    async def _verify_position_closed(self, slave_key: str, symbol: str, position_id: int) -> bool:
        """
        Quando CLOSE falha com 'Posição não encontrada', verifica via GET_POSITIONS
        se a posição realmente não existe mais (SL/TP/SO do broker fechou antes).
        Retorna True se resolvido (posição confirmada fechada), False se é erro real.
        """
        req_id = f"verify_{slave_key}_{position_id}_{time.time_ns()}"
        pos_response = await self.tcp_router.send_command_to_broker(
            slave_key, "GET_POSITIONS", {}, req_id
        )

        if pos_response.get("status") != "OK":
            logger.warning(f"    GET_POSITIONS falhou para {slave_key}, mantendo erro original")
            return False

        positions_count = pos_response.get("positions_count", 0)
        symbol_found = False
        for i in range(positions_count):
            prefix = f"pos_{i}_"
            pos_symbol = pos_response.get(f"{prefix}symbol", "")
            if pos_symbol == symbol:
                symbol_found = True
                break

        if not symbol_found:
            logger.info(f"    Confirmado: {symbol} não existe mais em {slave_key} (fechado por SL/TP/SO do broker)")
            self._on_close_success(position_id, slave_key, close_reason="BROKER_SLTP")
            self.copy_trade_log.emit(
                f"CLOSE [{slave_key}]: {symbol} já fechado pelo broker (SL/TP/SO)"
            )
            return True
        else:
            logger.error(
                f"    ERRO: {symbol} ainda existe em {slave_key} mas ticket não bateu! "
                f"Possível ticket mismatch para pos_id={position_id}"
            )
            return False

    # ── Callbacks de sucesso — mantém DB e position_map sincronizados ──

    def _on_open_success(self, position_id: int, slave_key: str,
                         slave_ticket: int, slave_lot: float,
                         symbol: str, master_volume: float, direction: str = "BUY"):
        """BUY/SELL confirmado pelo slave — cria row no open_positions e atualiza position_map."""
        now = time.time()
        # position_map: position_id → {slave_key: slave_ticket}
        if position_id not in self.position_map:
            self.position_map[position_id] = {}
        self.position_map[position_id][slave_key] = slave_ticket

        # Inserir row definitiva no open_positions (uma por slave, com dados reais)
        request_id = hashlib.sha256(
            f"{position_id}_{slave_key}_{int(now * 1000)}".encode()
        ).hexdigest()[:32]

        self.db.execute(
            """INSERT OR REPLACE INTO open_positions
               (master_ticket, slave_broker, slave_ticket, symbol,
                master_volume_original, master_volume_current, slave_volume_current,
                direction, status, request_id, opened_at, synced_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?)""",
            (position_id, slave_key, slave_ticket, symbol,
             master_volume, master_volume, slave_lot,
             direction, request_id, now, now)
        )
        self.db.commit()

    def _on_close_success(self, position_id: int, slave_key: str, close_reason: str = "COPYTRADE"):
        """Fechamento total confirmado — marcar CLOSED, limpar position_map."""
        now = time.time()
        self.db.execute(
            "UPDATE open_positions SET status = 'CLOSED', closed_at = ?, close_reason = ? WHERE master_ticket = ? AND slave_broker = ?",
            (now, close_reason, position_id, slave_key)
        )
        self.db.commit()
        if position_id in self.position_map:
            self.position_map[position_id].pop(slave_key, None)
            if not self.position_map[position_id]:
                del self.position_map[position_id]

    def _on_add_success(self, position_id: int, slave_key: str, added_volume: float, master_volume: float):
        """ADD à posição confirmado — incrementar volumes no DB."""
        now = time.time()
        self.db.execute(
            """UPDATE open_positions
               SET slave_volume_current = slave_volume_current + ?,
                   master_volume_current = master_volume_current + ?,
                   last_heartbeat = ?
               WHERE master_ticket = ? AND slave_broker = ? AND status = 'OPEN'""",
            (added_volume, master_volume, now, position_id, slave_key)
        )
        self.db.commit()

    def _on_partial_close_success(self, position_id: int, slave_key: str, closed_volume: float):
        """Fechamento parcial confirmado — decrementar volume do slave."""
        now = time.time()
        self.db.execute(
            """UPDATE open_positions
               SET slave_volume_current = MAX(0, slave_volume_current - ?), last_heartbeat = ?
               WHERE master_ticket = ? AND slave_broker = ? AND status = 'OPEN'""",
            (closed_volume, now, position_id, slave_key)
        )
        self.db.commit()

    # ──────────────────────────────────────────────
    # Bloco 4 - Fechamento de Emergência
    # ──────────────────────────────────────────────
    async def emergency_close_all(self):
        """Fecha TODAS as posições em TODOS os MT5s (master + slaves).

        Fase 1 — close direto por ticket (sem round-trip de POSITIONS):
          Lê master_positions e open_positions do DB e dispara todos os
          closes em paralelo (master + todos os slaves ao mesmo tempo).
          _emergency_active suprime replicação redundante do OnTrade do master.

        Fase 2 — reconciliação (#56):
          GET_POSITIONS em cada broker para garantir que nada ficou aberto.
          Fecha órfãos (posições não rastreadas ou cujo close falhou silenciosamente).
        """
        logger.warning("EMERGÊNCIA: Fechando todas as posições!")
        self._emergency_active = True
        self.copy_trade_log.emit("EMERGÊNCIA: Iniciando fechamento de todas as posições...")

        connected = set(self.broker_manager.get_connected_brokers())
        master_key = self.broker_manager.get_master_broker()
        total_closed = 0
        errors: list[str] = []

        # ── Fase 1: closes em paralelo a partir do DB ──────────────────────
        tasks: list = []  # (coro, label)

        # Master — lê de master_positions
        if master_key and master_key in connected:
            rows = self.db.execute(
                "SELECT position_id, symbol, volume FROM master_positions WHERE status = 'OPEN'"
            ).fetchall()
            for pos_id, symbol, volume in rows:
                pos = {"ticket": pos_id, "symbol": symbol, "volume": volume}
                tasks.append((self._emergency_close_one(master_key, master_key, pos),
                               master_key))

        # Slaves — lê de open_positions
        slave_rows = self.db.execute(
            "SELECT slave_broker, slave_ticket, symbol, slave_volume_current "
            "FROM open_positions WHERE status IN ('OPEN', 'SYNCING')"
        ).fetchall()
        for slave_broker, slave_ticket, symbol, volume in slave_rows:
            if slave_broker in connected and slave_ticket:
                pos = {"ticket": slave_ticket, "symbol": symbol, "volume": volume or 0.0}
                tasks.append((self._emergency_close_one(slave_broker, master_key, pos),
                               slave_broker))

        if tasks:
            coros, labels = zip(*tasks)
            results = await asyncio.gather(*coros, return_exceptions=True)
            for label, result in zip(labels, results):
                if isinstance(result, Exception):
                    logger.exception(f"Emergency close exceção em {label}: {result}")
                    errors.append(f"{label}: exceção {result}")
                else:
                    ok, err = result
                    if ok:
                        total_closed += 1
                    elif err:
                        errors.append(err)

        # ── Marcar DB como PANIC ───────────────────────────────────────────
        now = time.time()
        with self.db:
            self.db.execute(
                "UPDATE open_positions SET status='PANIC', closed_at=?, close_reason='EMERGENCY'"
                " WHERE status IN ('OPEN','SYNCING','CLOSING')",
                (now,)
            )
            self.db.execute(
                "UPDATE master_positions SET status='CLOSED', closed_at=? WHERE status='OPEN'",
                (now,)
            )
        logger.info("  📝 Todas as posições marcadas como PANIC no DB")

        self.position_map.clear()
        self._position_locks.clear()
        self._emergency_completed_at = time.time()

        # ── Fase 2: reconciliação — detecta e fecha órfãos (#56) ──────────
        recon_tasks = [
            self._emergency_reconcile(bk, master_key) for bk in connected
        ]
        recon_results = await asyncio.gather(*recon_tasks, return_exceptions=True)
        orphans_total = 0
        for bk, result in zip(list(connected), recon_results):
            if isinstance(result, Exception):
                logger.warning(f"Reconciliação falhou em {bk}: {result}")
            else:
                orphans, msgs = result
                orphans_total += orphans
                errors.extend(msgs)

        self._emergency_active = False

        grand_total = total_closed + orphans_total
        if errors:
            msg = f"Emergência: {grand_total} posição(ões) fechada(s). Avisos: {'; '.join(errors)}"
            self.emergency_completed.emit(False, msg)
        else:
            msg = f"EMERGÊNCIA concluída: {grand_total} posição(ões) fechada(s)."
            self.emergency_completed.emit(True, msg)

        self.copy_trade_log.emit(msg)
        logger.warning(msg)

    async def _emergency_reconcile(self, broker_key: str,
                                    master_key: str) -> tuple[int, list[str]]:
        """Fase 2: GET_POSITIONS para fechar posições órfãs não cobertas pelo DB."""
        request_id = f"positions_recon_{broker_key}_{time.time_ns()}"
        response = await self.tcp_router.send_command_to_broker(
            broker_key, "GET_POSITIONS", {}, request_id
        )
        if response.get("status") != "OK":
            logger.warning(f"Reconciliação: falha ao obter posições de {broker_key}")
            return 0, []

        count = response.get("positions_count", 0)
        if count == 0:
            return 0, []

        orphan_closed = 0
        msgs: list[str] = []
        close_tasks = []
        orphan_positions = []
        for i in range(count):
            prefix = f"pos_{i}_"
            ticket = response.get(f"{prefix}ticket", 0)
            symbol = response.get(f"{prefix}symbol", "")
            volume = response.get(f"{prefix}volume", 0.0)
            if not ticket:
                continue
            logger.warning(f"  Reconciliação: órfã em {broker_key}: {symbol} ticket={ticket}")
            self.copy_trade_log.emit(
                f"EMERGÊNCIA RECONCILIAÇÃO: {broker_key} {symbol} ticket={ticket} ainda aberta!")
            pos = {"ticket": ticket, "symbol": symbol, "volume": volume}
            close_tasks.append(self._emergency_close_one(broker_key, master_key, pos))
            orphan_positions.append(pos)

        if close_tasks:
            results = await asyncio.gather(*close_tasks, return_exceptions=True)
            for pos, result in zip(orphan_positions, results):
                if isinstance(result, Exception) or not result[0]:
                    err = str(result) if isinstance(result, Exception) else result[1]
                    msgs.append(f"{broker_key}/{pos['symbol']} órfã não fechada: {err}")
                else:
                    orphan_closed += 1

        return orphan_closed, msgs

    async def _emergency_close_one(self, broker_key: str, master_broker: str,
                                    pos: dict) -> tuple[bool, str]:
        """Fecha uma posição específica. Retorna (sucesso, mensagem_erro)."""
        ticket = pos["ticket"]
        symbol = pos["symbol"]
        volume = pos["volume"]

        close_id = f"close_{broker_key}_{ticket}_{time.time_ns()}"
        close_response = await self.tcp_router.send_command_to_broker(
            broker_key, "TRADE_POSITION_CLOSE_ID",
            {"ticket": ticket, "emergency": True}, close_id,
        )

        if close_response.get("status") == "OK":
            deal_ticket = close_response.get("deal", 0) or close_response.get("order", 0)
            self._insert_history(
                master_broker or broker_key, deal_ticket, symbol,
                "EMERGENCY_CLOSE", volume,
                broker_key, ticket, volume, "SUCCESS",
                close_reason="EMERGENCY",
            )
            self.copy_trade_log.emit(
                f"EMERGÊNCIA: Fechado {symbol} ticket={ticket} em {broker_key}")
            return True, ""

        error = close_response.get("error_message", "") or close_response.get("message", "erro")
        self._insert_history(
            master_broker or broker_key, ticket, symbol,
            "EMERGENCY_CLOSE", volume,
            broker_key, ticket, volume, "FAILED", error,
            close_reason="EMERGENCY",
        )
        return False, f"{broker_key}/{symbol}: {error}"

    # ──────────────────────────────────────────────
    # Bloco 5 - Histórico e Estatísticas (SQLite)
    # ──────────────────────────────────────────────
    def _insert_history(self, master_broker, master_ticket, symbol, action,
                        master_lot, slave_broker, slave_ticket, slave_lot, status,
                        error_message="", close_reason=""):
        cursor = self.db.execute(
            """INSERT INTO copytrade_history
               (timestamp, master_broker, master_ticket, symbol, action,
                master_lot, slave_broker, slave_ticket, slave_lot, status, error_message, close_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (time.time(), master_broker, master_ticket, symbol, action,
             master_lot, slave_broker, slave_ticket, slave_lot, status, error_message, close_reason)
        )
        self.db.commit()
        return cursor.lastrowid

    def _update_history(self, record_id, status, slave_ticket=None, error_message="", close_reason=""):
        if slave_ticket is not None:
            self.db.execute(
                "UPDATE copytrade_history SET status=?, slave_ticket=?, error_message=?, close_reason=? WHERE id=?",
                (status, slave_ticket, error_message, close_reason, record_id)
            )
        else:
            self.db.execute(
                "UPDATE copytrade_history SET status=?, error_message=?, close_reason=? WHERE id=?",
                (status, error_message, close_reason, record_id)
            )
        self.db.commit()

    def _engine_ready(self) -> bool:
        """True se o motor existe e ainda está rodando. Durante o shutdown a
        GUI pode disparar requests (timers/sinais enfileirados) depois do
        engine.stop() — submeter aí levantaria RuntimeError."""
        return self.engine is not None and self.engine.is_running

    def request_trade_history(self, broker_key=None, limit=100):
        """Fire-and-forget. Resposta via signal `trade_history_ready`."""
        if not self._engine_ready():
            logger.debug("request_trade_history: motor indisponível — ignorado.")
            return
        self.engine.submit(self._fetch_trade_history(broker_key, limit))

    async def _fetch_trade_history(self, broker_key, limit):
        try:
            if broker_key:
                rows = self.db.execute(
                    """SELECT * FROM copytrade_history
                       WHERE master_broker=? OR slave_broker=?
                       ORDER BY timestamp DESC LIMIT ?""",
                    (broker_key, broker_key, limit)
                ).fetchall()
            else:
                rows = self.db.execute(
                    "SELECT * FROM copytrade_history ORDER BY timestamp DESC LIMIT ?",
                    (limit,)
                ).fetchall()
            columns = ["id", "timestamp", "master_broker", "master_ticket", "symbol",
                       "action", "master_lot", "slave_broker", "slave_ticket",
                       "slave_lot", "status", "error_message", "close_reason"]
            result = [dict(zip(columns, row)) for row in rows]
        except Exception as e:
            logger.error(f"_fetch_trade_history falhou: {e}")
            result = []
        self.trade_history_ready.emit(result, broker_key or "")

    def request_today_stats(self):
        """Fire-and-forget. Resposta via signal `today_stats_ready`."""
        if not self._engine_ready():
            logger.debug("request_today_stats: motor indisponível — ignorado.")
            return
        self.engine.submit(self._fetch_today_stats())

    def clear_trade_history(self, start_ts=None, end_ts=None):
        """Apaga registros de `copytrade_history`. Fire-and-forget.

        - start_ts e end_ts None → apaga tudo
        - ambos definidos → apaga o intervalo [start_ts, end_ts]
        - só start_ts → apaga timestamp >= start_ts
        - só end_ts → apaga timestamp <= end_ts
        Resposta via signal `history_cleared`.
        """
        if not self._engine_ready():
            logger.debug("clear_trade_history: motor indisponível — ignorado.")
            return
        self.engine.submit(self._clear_trade_history(start_ts, end_ts))

    async def _clear_trade_history(self, start_ts, end_ts):
        try:
            if start_ts is None and end_ts is None:
                cur = self.db.execute("DELETE FROM copytrade_history")
            elif start_ts is not None and end_ts is not None:
                cur = self.db.execute(
                    "DELETE FROM copytrade_history WHERE timestamp BETWEEN ? AND ?",
                    (start_ts, end_ts))
            elif start_ts is not None:
                cur = self.db.execute(
                    "DELETE FROM copytrade_history WHERE timestamp >= ?", (start_ts,))
            else:
                cur = self.db.execute(
                    "DELETE FROM copytrade_history WHERE timestamp <= ?", (end_ts,))
            deleted = cur.rowcount
            self.db.commit()
            logger.info(f"Histórico limpo: {deleted} registro(s) removido(s).")
        except Exception as e:
            logger.error(f"_clear_trade_history falhou: {e}")
            deleted = 0
        self.history_cleared.emit(deleted)

    async def _fetch_today_stats(self):
        today_start = datetime.datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp()
        try:
            row = self.db.execute(
                """SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN status='SUCCESS' THEN 1 ELSE 0 END) as success,
                    SUM(CASE WHEN status='FAILED' THEN 1 ELSE 0 END) as failed
                   FROM copytrade_history WHERE timestamp >= ?""",
                (today_start,)
            ).fetchone()
            stats = {"total": row[0] or 0, "success": row[1] or 0, "failed": row[2] or 0}
        except Exception as e:
            logger.error(f"_fetch_today_stats falhou: {e}")
            stats = {"total": 0, "success": 0, "failed": 0}
        self.today_stats_ready.emit(stats)

    # ──────────────────────────────────────────────
    # Bloco 6 - Shutdown
    # ──────────────────────────────────────────────
    def count_open_positions(self) -> int:
        """Conta operações de copytrade abertas NESTA sessão, lendo o
        position_map em memória.

        NÃO usa as tabelas persistidas master_positions/open_positions: elas
        podem reter linhas com status OPEN obsoletas de uma sessão anterior
        encerrada de forma anormal (crash, fechamento pelo broker não
        reconciliado) — o que gerava alerta falso de 'operações abertas' no
        fechamento, mesmo sem nenhuma posição real do copytrade aberta.

        position_map só contém posições do copytrade (magic) abertas e ainda
        não fechadas; posições alienígenas/manuais nunca entram nele.
        len() de dict é atômico sob o GIL — seguro ler da thread da GUI."""
        return len(self.position_map)

    def close(self):
        """Fecha a conexão SQLite. No Windows, conexão aberta mantém o
        arquivo locked. Submete ao motor (single-threaded asyncio) e
        bloqueia até concluir ou estourar timeout."""
        if self.engine is None:
            self._do_close()
            return
        try:
            fut = self.engine.submit(self._async_close())
            fut.result(timeout=2.0)
        except Exception as e:
            logger.warning(f"async_close timeout/erro: {e}")

    async def _async_close(self):
        self._do_close()

    def _do_close(self):
        if self.db is None:
            return
        try:
            self.db.close()
            logger.info("Conexão SQLite fechada.")
        except Exception as e:
            logger.warning(f"Erro ao fechar DB: {e}")
        finally:
            self.db = None
