"""Scraping de la página ethinfo.asp de una ONT Huawei y exposición en formato Prometheus.

Flujo de sesión (equivalente a monitor2_v1.sh):
  1. POST /asp/GetRandCount.asp            -> token (hex) + cookies
  2. POST /login.cgi                       -> sesión autenticada
  3. GET  /html/amp/ethinfo/ethinfo.asp    -> HTML con <script> (LANStats / GEInfo)
  4. POST /asp/GetRandCount.asp            -> token fresco
  5. POST /logout.cgi?RequestFile=...      -> cierre de sesión (solo al apagar)
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field

import httpx
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily
from prometheus_client.registry import Collector

from config import Settings

log = logging.getLogger("huawei_ont.collector")

INT_MAX = 4294967296  # intMax en el JS de la ONT (2^32)

# Código de velocidad de GEInfo.Speed -> Mbps
SPEED_MAP_MBPS: dict[int, int] = {0: 10, 1: 100, 2: 1000, 3: 2500, 4: 10000, 6: 5000}

_LANSTATS_RE = re.compile(r"new\s+LANStats\s*\((.*?)\)", re.S)
_GEINFO_RE = re.compile(r"new\s+GEInfo\s*\((.*?)\)", re.S)
# Argumentos JS: "doble", 'simple' o literal sin comillas
_ARG_RE = re.compile(r'"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\'|([^,\s][^,]*)')
_JS_ESCAPE_RE = re.compile(r"\\(x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|.)", re.S)
_PARSEINT_RE = re.compile(r"\s*([+-]?\d+)")
_PORT_RE = re.compile(r"LANEthernetInterfaceConfig\.(\d+)", re.I)


class SessionExpiredError(Exception):
    """La ONT devolvió el login u otro HTML sin los datos esperados."""


# --------------------------------------------------------------------------- #
# Parsing del JavaScript
# --------------------------------------------------------------------------- #
def _js_unescape(s: str) -> str:
    def repl(m: re.Match[str]) -> str:
        esc = m.group(1)
        if esc[0] in "xu" and len(esc) > 1:
            return chr(int(esc[1:], 16))
        return {"n": "\n", "t": "\t", "r": "\r"}.get(esc, esc)

    return _JS_ESCAPE_RE.sub(repl, s)


def _split_args(raw: str) -> list[str]:
    args: list[str] = []
    for m in _ARG_RE.finditer(raw):
        dq, sq, bare = m.groups()
        if dq is not None:
            args.append(_js_unescape(dq))
        elif sq is not None:
            args.append(_js_unescape(sq))
        else:
            args.append(bare.strip())
    return args


def js_parse_int(value: str) -> int:
    """Equivalente a parseInt() de JS (base 10); NaN se trata como 0."""
    m = _PARSEINT_RE.match(value or "")
    return int(m.group(1)) if m else 0


def _port_name(domain: str, index: int) -> str:
    m = _PORT_RE.search(domain)
    return f"LAN{m.group(1)}" if m else f"LAN{index + 1}"


@dataclass(frozen=True)
class PortStats:
    port: str
    tx_bytes: int
    rx_bytes: int
    tx_packets: int
    rx_packets: int


@dataclass(frozen=True)
class PortLink:
    port: str
    status: int  # 1 = UP, 0 = DOWN
    speed_mbps: int | None
    mode: str


def parse_lan_stats(html: str) -> list[PortStats]:
    result: list[PortStats] = []
    for idx, m in enumerate(_LANSTATS_RE.finditer(html)):
        a = _split_args(m.group(1))
        if len(a) < 13:
            log.debug("LANStats con %d argumentos ignorado: %r", len(a), m.group(1))
            continue
        (domain, tx_p, tx_p_h, tx_gb, tx_gb_h, tx_bb, tx_bb_h,
         rx_p, rx_p_h, rx_gb, rx_gb_h, rx_bb, rx_bb_h) = a[:13]
        p = js_parse_int
        tx_bytes = p(tx_gb_h) * INT_MAX + p(tx_gb) + p(tx_bb) + p(tx_bb_h) * INT_MAX
        rx_bytes = p(rx_gb_h) * INT_MAX + p(rx_gb) + p(rx_bb) + p(rx_bb_h) * INT_MAX
        tx_packets = p(tx_p_h) * INT_MAX + p(tx_p)
        rx_packets = p(rx_p_h) * INT_MAX + p(rx_p)
        result.append(PortStats(_port_name(domain, idx), tx_bytes, rx_bytes, tx_packets, rx_packets))
    return result


def parse_ge_infos(html: str) -> list[PortLink]:
    result: list[PortLink] = []
    for idx, m in enumerate(_GEINFO_RE.finditer(html)):
        a = _split_args(m.group(1))
        if len(a) < 4:
            log.debug("GEInfo con %d argumentos ignorado: %r", len(a), m.group(1))
            continue
        domain, mode, speed, status = a[:4]
        speed_code = js_parse_int(speed)
        speed_mbps = SPEED_MAP_MBPS.get(speed_code)
        if speed_mbps is None:
            log.debug("Código de velocidad desconocido %r en %s", speed, domain)
        result.append(
            PortLink(
                port=_port_name(domain, idx),
                status=1 if js_parse_int(status) == 1 else 0,
                speed_mbps=speed_mbps,
                mode=mode,
            )
        )
    return result


# --------------------------------------------------------------------------- #
# Estado expuesto
# --------------------------------------------------------------------------- #
@dataclass
class Snapshot:
    stats: dict[str, PortStats] = field(default_factory=dict)
    links: dict[str, PortLink] = field(default_factory=dict)
    rx_bps: dict[str, float] = field(default_factory=dict)
    tx_bps: dict[str, float] = field(default_factory=dict)


class HuaweiOntCollector(Collector):
    """Mantiene la sesión con la ONT, hace polling en segundo plano y expone métricas."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = self._new_client()
        self._session_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None

        self._snapshot = Snapshot()
        # puerto -> (instante monotónico, rx_bytes, tx_bytes) de la muestra anterior
        self._prev: dict[str, tuple[float, int, int]] = {}

        self.logged_in = False
        self.last_success_ts: float | None = None  # epoch
        self.last_error: str | None = None
        self.scrape_ok = False
        self.scrape_duration = 0.0
        self.logins_total = 0
        self.scrape_errors_total = 0

    def _new_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.settings.base_url,
            timeout=self.settings.request_timeout_seconds,
            follow_redirects=False,
            headers={"User-Agent": "Mozilla/5.0 (huawei-ont-exporter)"},
        )

    # ------------------------------------------------------------------ #
    # Ciclo de vida
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        try:
            await self.login()
        except Exception as exc:  # noqa: BLE001 - el loop reintentará
            self.last_error = f"login inicial: {exc!r}"
            log.error("Login inicial fallido (%s); se reintentará en el polling", exc)
        self._task = asyncio.create_task(self._poll_loop(), name="huawei-ont-poller")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        try:
            await self.logout()
        finally:
            await self._client.aclose()

    # ------------------------------------------------------------------ #
    # Sesión
    # ------------------------------------------------------------------ #
    async def _get_token(self) -> str:
        r = await self._client.post("/asp/GetRandCount.asp")
        r.raise_for_status()
        token = re.sub(r"[^a-fA-F0-9]", "", r.text)  # == tr -cd 'a-fA-F0-9'
        if not token:
            raise RuntimeError("GetRandCount.asp no devolvió token")
        return token

    async def login(self) -> None:
        async with self._session_lock:
            await self._login_unlocked()

    async def _login_unlocked(self) -> None:
        s = self.settings
        self._client.cookies.clear()
        self.logged_in = False
        token = await self._get_token()
        r = await self._client.post(
            "/login.cgi",
            data={
                "UserName": s.ont_username,
                "PassWord": s.login_password,
                "Language": s.ont_language,
                "x.X_HW_Token": token,
            },
            headers={"Origin": s.base_url, "Referer": f"{s.base_url}/"},
        )
        if r.status_code >= 400:
            raise RuntimeError(f"login.cgi respondió HTTP {r.status_code}")
        self.logins_total += 1
        self.logged_in = True
        log.info("Sesión iniciada en la ONT %s como %s", s.ont_ip, s.ont_username)

    async def logout(self) -> None:
        if not self.logged_in:
            return
        async with self._session_lock:
            s = self.settings
            try:
                token = await self._get_token()
                await self._client.post(
                    "/logout.cgi",
                    params={"RequestFile": "html/logout.html"},
                    data={"x.X_HW_Token": token},
                    headers={"Origin": s.base_url, "Referer": f"{s.base_url}/index.asp"},
                )
                log.info("Sesión cerrada en la ONT %s", s.ont_ip)
            except Exception as exc:  # noqa: BLE001
                log.warning("No se pudo cerrar la sesión limpiamente: %s", exc)
            finally:
                self.logged_in = False
                self._client.cookies.clear()

    # ------------------------------------------------------------------ #
    # Obtención de datos
    # ------------------------------------------------------------------ #
    async def _get_ethinfo_once(self) -> str:
        r = await self._client.get(
            "/html/amp/ethinfo/ethinfo.asp",
            headers={"Referer": f"{self.settings.base_url}/configindex.asp"},
        )
        if r.is_redirect:
            raise SessionExpiredError(f"redirección a {r.headers.get('location')}")
        if r.status_code != 200:
            raise SessionExpiredError(f"HTTP {r.status_code}")
        if "LANStats" not in r.text or "userEthInfos" not in r.text:
            raise SessionExpiredError("respuesta sin userEthInfos (¿página de login?)")
        return r.text

    async def fetch_ethinfo(self) -> str:
        """Descarga ethinfo.asp; si la sesión caducó o falla, re-login y un reintento."""
        async with self._session_lock:
            if not self.logged_in:
                await self._login_unlocked()
            try:
                return await self._get_ethinfo_once()
            except (SessionExpiredError, httpx.HTTPError) as exc:
                log.warning("Fallo al leer ethinfo.asp (%s); re-autenticando", exc)
                await self._login_unlocked()
                return await self._get_ethinfo_once()

    # ------------------------------------------------------------------ #
    # Polling
    # ------------------------------------------------------------------ #
    async def _poll_loop(self) -> None:
        interval = self.settings.poll_interval_seconds
        failures = 0
        while True:
            started = time.monotonic()
            try:
                await self.scrape_once()
                failures = 0
                delay = max(0.0, interval - (time.monotonic() - started))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                failures += 1
                self.scrape_ok = False
                self.scrape_errors_total += 1
                self.last_error = repr(exc)
                # Limpiar throughput para no exponer valores obsoletos
                self._snapshot = Snapshot(stats=self._snapshot.stats, links=self._snapshot.links)
                delay = min(interval * 2 ** (failures - 1), self.settings.max_backoff_seconds)
                log.error("Scrape fallido (#%d): %s. Reintento en %.1fs", failures, exc, delay)
            await asyncio.sleep(delay)

    async def scrape_once(self) -> None:
        t0 = time.monotonic()
        html = await self.fetch_ethinfo()
        now = time.monotonic()

        stats = parse_lan_stats(html)
        if not stats:
            raise SessionExpiredError("no se encontraron instancias LANStats")
        links = parse_ge_infos(html)

        rx_bps: dict[str, float] = {}
        tx_bps: dict[str, float] = {}
        for st in stats:
            prev = self._prev.get(st.port)
            if prev is not None:
                prev_t, prev_rx, prev_tx = prev
                dt = now - prev_t
                if dt > 0 and st.rx_bytes >= prev_rx and st.tx_bytes >= prev_tx:
                    rx_bps[st.port] = (st.rx_bytes - prev_rx) / dt
                    tx_bps[st.port] = (st.tx_bytes - prev_tx) / dt
                else:
                    log.info("Contadores reiniciados en %s; se descarta la muestra", st.port)
            self._prev[st.port] = (now, st.rx_bytes, st.tx_bytes)

        # Sustitución atómica del snapshot
        self._snapshot = Snapshot(
            stats={s.port: s for s in stats},
            links={l.port: l for l in links},
            rx_bps=rx_bps,
            tx_bps=tx_bps,
        )
        self.scrape_ok = True
        self.last_error = None
        self.last_success_ts = time.time()
        self.scrape_duration = time.monotonic() - t0
        log.debug("Scrape OK: %d puertos en %.3fs", len(stats), self.scrape_duration)

    # ------------------------------------------------------------------ #
    # Prometheus
    # ------------------------------------------------------------------ #
    def collect(self):
        snap = self._snapshot
        lbl = ["port"]

        rx_b = CounterMetricFamily("huawei_ont_rx_bytes", "Bytes recibidos por puerto LAN", labels=lbl)
        tx_b = CounterMetricFamily("huawei_ont_tx_bytes", "Bytes transmitidos por puerto LAN", labels=lbl)
        rx_p = CounterMetricFamily("huawei_ont_rx_packets", "Paquetes recibidos por puerto LAN", labels=lbl)
        tx_p = CounterMetricFamily("huawei_ont_tx_packets", "Paquetes transmitidos por puerto LAN", labels=lbl)
        for port, st in sorted(snap.stats.items()):
            rx_b.add_metric([port], st.rx_bytes)
            tx_b.add_metric([port], st.tx_bytes)
            rx_p.add_metric([port], st.rx_packets)
            tx_p.add_metric([port], st.tx_packets)
        yield from (rx_b, tx_b, rx_p, tx_p)

        status = GaugeMetricFamily("huawei_ont_port_status", "Estado del enlace (1=UP, 0=DOWN)", labels=lbl)
        speed = GaugeMetricFamily("huawei_ont_port_speed_mbps", "Velocidad negociada en Mbps", labels=lbl)
        for port, ln in sorted(snap.links.items()):
            status.add_metric([port], ln.status)
            if ln.speed_mbps is not None:
                speed.add_metric([port], ln.speed_mbps)
        yield from (status, speed)

        rx_t = GaugeMetricFamily(
            "huawei_ont_rx_throughput_bytes_per_second", "Throughput RX calculado entre muestras", labels=lbl
        )
        tx_t = GaugeMetricFamily(
            "huawei_ont_tx_throughput_bytes_per_second", "Throughput TX calculado entre muestras", labels=lbl
        )
        for port in sorted(snap.rx_bps):
            rx_t.add_metric([port], snap.rx_bps[port])
            tx_t.add_metric([port], snap.tx_bps[port])
        yield from (rx_t, tx_t)

        # Métricas de salud del propio exporter
        yield GaugeMetricFamily("huawei_ont_up", "1 si el último scrape a la ONT fue exitoso", value=int(self.scrape_ok))
        yield GaugeMetricFamily(
            "huawei_ont_scrape_duration_seconds", "Duración del último scrape exitoso", value=self.scrape_duration
        )
        if self.last_success_ts is not None:
            yield GaugeMetricFamily(
                "huawei_ont_last_success_timestamp_seconds",
                "Epoch del último scrape exitoso",
                value=self.last_success_ts,
            )
        yield CounterMetricFamily("huawei_ont_logins", "Logins realizados contra la ONT", value=self.logins_total)
        yield CounterMetricFamily(
            "huawei_ont_scrape_errors", "Ciclos de scrape fallidos", value=self.scrape_errors_total
        )

    # ------------------------------------------------------------------ #
    # Health
    # ------------------------------------------------------------------ #
    def health(self) -> dict:
        stale_after = max(3 * self.settings.poll_interval_seconds, 30)
        age = None if self.last_success_ts is None else time.time() - self.last_success_ts
        healthy = age is not None and age <= stale_after
        return {
            "status": "ok" if healthy else "degraded",
            "ont_ip": self.settings.ont_ip,
            "logged_in": self.logged_in,
            "last_scrape_ok": self.scrape_ok,
            "last_success_age_seconds": None if age is None else round(age, 2),
            "ports": sorted(self._snapshot.stats),
            "last_error": self.last_error,
        }
