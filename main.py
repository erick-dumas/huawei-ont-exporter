"""Exporter Prometheus para puertos LAN de ONT Huawei (FastAPI)."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest

from collector import HuaweiOntCollector
from config import get_settings

settings = get_settings()

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("huawei_ont")

registry = CollectorRegistry(auto_describe=False)


@asynccontextmanager
async def lifespan(app: FastAPI):
    collector = HuaweiOntCollector(settings)
    registry.register(collector)
    app.state.collector = collector
    log.info(
        "Iniciando exporter: ONT=%s intervalo=%ss", settings.ont_ip, settings.poll_interval_seconds
    )
    await collector.start()
    try:
        yield
    finally:
        log.info("Apagando: cerrando sesión en la ONT")
        await collector.stop()
        registry.unregister(collector)


app = FastAPI(title="Huawei ONT Exporter", version="1.0.0", lifespan=lifespan)


@app.get("/metrics")
async def metrics() -> Response:
    return Response(content=generate_latest(registry), media_type=CONTENT_TYPE_LATEST)


@app.get("/health")
async def health() -> JSONResponse:
    info = app.state.collector.health()
    return JSONResponse(info, status_code=200 if info["status"] == "ok" else 503)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host=settings.app_host, port=settings.app_port, log_level=settings.log_level.lower())
