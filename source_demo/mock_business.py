"""只读目标服务。单独监听本机 9100，客户不能传入任意目标 URL。"""

from __future__ import annotations

import os
from datetime import date

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse

app = FastAPI(title="Demo Mock Business")


@app.get("/demo/orders", response_class=HTMLResponse)
def demo_page():
    return '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>订单验证页面</title>
    <h1>订单查询验证</h1><label>订单状态 <select id="status"><option value="pending">待处理</option><option value="completed">已完成</option></select></label>
    <button id="query" onclick="document.getElementById('result').textContent = document.getElementById('status').value === 'pending' ? '订单数量：2' : '订单数量：3'">查询订单</button><p id="result" role="status">等待查询</p></html>'''

ORDERS = {
    "tenant-a": [("2026-09-01", "completed"), ("2026-09-02", "completed"),
                 ("2026-09-03", "completed"), ("2026-09-04", "pending"),
                 ("2026-09-05", "pending")],
    "tenant-b": [("2026-09-01", "completed"), ("2026-09-02", "completed"),
                 ("2026-09-03", "completed"), ("2026-09-04", "completed"),
                 ("2026-09-05", "pending"), ("2026-09-06", "pending"),
                 ("2026-09-07", "pending")],
}


def summary(tenant: str, status: str, date_from: str, date_to: str) -> dict:
    if tenant not in ORDERS or status not in ("all", "pending", "completed"):
        raise ValueError("目标范围无效")
    start, end = date.fromisoformat(date_from), date.fromisoformat(date_to)
    if start > end or (end - start).days > 31:
        raise ValueError("日期范围无效")
    count = sum(start <= date.fromisoformat(day) <= end and (status == "all" or state == status)
                for day, state in ORDERS[tenant])
    return {"tenant": tenant, "filter": {"status": status, "date_from": date_from,
                                           "date_to": date_to}, "total": count}


@app.get("/orders/summary")
def get_summary(tenant: str, status: str, date_from: str, date_to: str,
                x_mock_secret: str = Header(default=""), fault: str = Query(default="")) -> dict:
    if x_mock_secret != os.getenv("MOCK_SECRET", "demo-mock-only"):
        raise HTTPException(403, "forbidden")
    if fault == "timeout" and os.getenv("MOCK_ENABLE_FAULTS") == "true":
        raise HTTPException(504, "simulated timeout")
    try:
        return summary(tenant, status, date_from, date_to)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
