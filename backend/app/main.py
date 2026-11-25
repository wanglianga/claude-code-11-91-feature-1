from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .routers import fleet, operations, reservations, resources, search

app = FastAPI(
    title=settings.app_name,
    version="1.1.0",
    description=(
        "充电预约演示系统：固定枪口 / 资源池两种预约粒度、事务级容量重算、"
        "幂等确认、维护影响分析，以及车队批次（逐车确认 / 原子组合两条路线、"
        "最低就绪数与取消/失能/换车后的即时重算）。"
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(resources.router)
app.include_router(search.router)
app.include_router(reservations.router)
app.include_router(operations.router)
app.include_router(fleet.router)


@app.get("/health", tags=["health"])
def health():
    return {"status": "ok"}
