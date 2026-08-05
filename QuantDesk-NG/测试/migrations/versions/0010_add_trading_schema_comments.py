"""Add comments to every trading, market, alert, and state column.

Revision ID: 0010_trading_schema_comments
Revises: 0009_multitenant_trading
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0010_trading_schema_comments"
down_revision: str | None = "0009_multitenant_trading"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TABLE_COMMENTS = {
    "alerts": "按用户隔离的行情、评分及模拟盘提醒记录",
    "klines": "系统共享的合约历史 K 线行情",
    "news": "系统共享的新闻、翻译、情绪和摘要数据",
    "paper_accounts": "用户独立运行并绑定策略快照的模拟盘账户",
    "paper_equity": "按模拟盘隔离的历史权益曲线",
    "paper_positions": "按用户和模拟盘隔离的当前模拟持仓",
    "paper_trades": "按用户和模拟盘隔离的已平仓模拟成交",
    "positions": "按用户隔离的 Binance 实盘持仓快照",
    "scores": "系统共享的多周期量化评分及因子明细",
    "social": "系统共享的社交平台情绪和热度快照",
    "system_state": "行情采集和全市场任务使用的系统级运行状态",
    "ticker": "系统共享的合约最新价格和 24 小时统计快照",
    "user_states": "按用户隔离的偏好、游标和运行状态",
}


COLUMN_COMMENTS = {
    "alerts": {
        "id": "提醒记录主键，自增",
        "user_id": "所属用户 ID，用于租户隔离",
        "ts": "提醒产生时间的 Unix 时间戳",
        "symbol": "提醒关联的交易标的代码",
        "kind": "提醒类型，例如评分、异动、模拟开仓或模拟平仓",
        "direction": "提醒方向，例如 long 多头或 short 空头",
        "score": "触发提醒时的量化评分，可为空",
        "message": "面向用户展示的提醒正文",
        "detail": "提醒因子、模拟盘标识等 JSON 扩展明细",
        "read": "当前用户是否已读该提醒",
    },
    "klines": {
        "symbol": "交易标的代码",
        "tf": "K 线周期，例如 15m、1h 或 4h",
        "open_time": "K 线开盘时间的 Unix 毫秒时间戳",
        "open": "本周期开盘价",
        "high": "本周期最高价",
        "low": "本周期最低价",
        "close": "本周期收盘价",
        "volume": "本周期成交量",
    },
    "news": {
        "id": "新闻稳定标识主键",
        "ts": "新闻发布时间的 Unix 时间戳",
        "source": "新闻来源名称",
        "lang": "新闻原文语言代码",
        "title": "新闻原始标题",
        "title_zh": "新闻中文翻译标题",
        "link": "新闻原文链接",
        "sentiment": "新闻情绪分类",
        "summary": "新闻摘要或深度舆情摘要",
    },
    "paper_accounts": {
        "id": "模拟盘内部主键，自增",
        "public_id": "API 对外使用的模拟盘 UUID",
        "user_id": "所属用户 ID，用于租户隔离",
        "strategy_id": "同一用户拥有的绑定策略内部 ID",
        "name": "用户自定义的模拟盘名称，同一用户内唯一",
        "status": "模拟盘状态：active、paused 或 archived",
        "initial_balance": "本轮模拟盘初始资金",
        "balance": "模拟盘当前可用余额",
        "config_json": "杠杆、仓位、成本和风控配置快照",
        "strategy_snapshot_json": "创建模拟盘时绑定的完整策略版本快照",
        "started_at": "本轮模拟盘开始时间（UTC）",
        "last_tick_at": "后台策略最后一次执行时间（UTC）",
        "created_at": "模拟盘创建时间（UTC）",
        "updated_at": "模拟盘最后更新时间（UTC）",
    },
    "paper_equity": {
        "paper_account_id": "所属模拟盘内部 ID",
        "user_id": "所属用户 ID，用于租户一致性校验",
        "ts": "权益采样时间的 Unix 时间戳",
        "equity": "采样时账户总权益",
        "balance": "采样时账户可用余额",
    },
    "paper_positions": {
        "id": "模拟持仓主键，自增",
        "paper_account_id": "所属模拟盘内部 ID",
        "user_id": "所属用户 ID，用于租户一致性校验",
        "symbol": "模拟持仓交易标的代码",
        "side": "持仓方向：1 为多头，-1 为空头",
        "qty": "当前持仓数量",
        "avg_entry": "持仓平均开仓价格",
        "margin": "当前占用模拟保证金",
        "leverage": "逐仓杠杆倍数",
        "stop": "止损触发价格",
        "target": "止盈触发价格",
        "adds": "开仓后追加仓位次数",
        "opened_ts": "首次开仓时间的 Unix 时间戳",
        "last_add_ts": "最后一次追加仓位时间的 Unix 时间戳",
        "open_score": "首次开仓时的量化评分",
        "basis": "开仓理由和信号因子的 JSON 明细",
        "funding_acc": "持仓期间累计资金费用",
        "liq_price": "模拟强平价格",
        "funding_ts": "最后一次计算资金费用的 Unix 时间戳",
        "atr_entry": "开仓时的 ATR 波动指标值",
        "peak_price": "持仓期间用于跟踪止盈的最优价格",
        "tp_done": "是否已执行阶段性止盈",
    },
    "paper_trades": {
        "id": "模拟成交主键，自增",
        "paper_account_id": "所属模拟盘内部 ID",
        "user_id": "所属用户 ID，用于租户一致性校验",
        "symbol": "模拟成交交易标的代码",
        "side": "成交方向：1 为多头，-1 为空头",
        "qty": "本次平仓的持仓数量",
        "entry_price": "开仓成交价格",
        "exit_price": "平仓成交价格",
        "margin": "本次成交占用的模拟保证金",
        "pnl": "扣除资金费用前后按引擎口径计算的已实现盈亏",
        "fee": "本次开平仓模拟手续费",
        "funding": "持仓期间累计模拟资金费用",
        "reason": "平仓原因，例如止损、止盈、反转、超时或强平",
        "open_score": "开仓时的量化评分",
        "opened_ts": "开仓时间的 Unix 时间戳",
        "closed_ts": "平仓时间的 Unix 时间戳",
    },
    "positions": {
        "user_id": "所属用户 ID，用于租户隔离",
        "symbol": "Binance 实盘持仓交易标的代码",
        "amt": "实盘持仓数量的绝对值",
        "side": "实盘持仓方向：long 多头或 short 空头",
        "entry_price": "实盘持仓平均开仓价格",
        "mark_price": "Binance 标记价格",
        "upnl": "实盘持仓未实现盈亏",
        "leverage": "实盘持仓杠杆倍数",
        "ts": "Binance 持仓数据源更新时间戳",
    },
    "scores": {
        "symbol": "评分对应的交易标的代码",
        "tf": "评分对应的 K 线周期",
        "open_time": "评分对应 K 线开盘时间的 Unix 毫秒时间戳",
        "score": "多空综合评分，正数偏多、负数偏空",
        "detail": "各评分因子及理由的 JSON 明细",
    },
    "social": {
        "symbol": "社交情绪关联的交易标的代码",
        "st_bull": "Stocktwits 看多消息数量",
        "st_bear": "Stocktwits 看空消息数量",
        "st_msgs": "Stocktwits 统计消息总数",
        "ape_mentions": "ApeWisdom 提及次数",
        "ape_upvotes": "ApeWisdom 相关内容点赞数",
        "ape_rank": "ApeWisdom 当前热度排名",
        "ape_rank_24h": "ApeWisdom 过去 24 小时热度排名",
        "ts": "社交数据采集时间的 Unix 时间戳",
    },
    "system_state": {
        "k": "系统级状态键",
        "v": "JSON 序列化后的系统级状态值",
    },
    "ticker": {
        "symbol": "交易标的代码",
        "price": "最新成交价格",
        "pct_24h": "过去 24 小时价格涨跌幅百分比",
        "quote_volume": "过去 24 小时报价资产成交额",
        "ts": "行情快照采集时间的 Unix 时间戳",
    },
    "user_states": {
        "user_id": "所属用户 ID，用于租户隔离",
        "k": "用户状态键",
        "v": "JSON 序列化后的用户状态值",
        "updated_at": "用户状态最后更新时间（UTC）",
    },
}


def _alter_comments(comments: dict[str, dict[str, str | None]]) -> None:
    bind = op.get_bind()
    if bind.dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL")
    inspector = sa.inspect(bind)
    available_tables = set(inspector.get_table_names())
    for table_name, column_comments in comments.items():
        if table_name not in available_tables:
            raise RuntimeError(f"required table is missing: {table_name}")
        columns: dict[str, dict[str, Any]] = {
            column["name"]: column for column in inspector.get_columns(table_name)
        }
        for column_name, comment in column_comments.items():
            column = columns.get(column_name)
            if column is None:
                raise RuntimeError(f"required column is missing: {table_name}.{column_name}")
            default = column.get("default")
            op.alter_column(
                table_name,
                column_name,
                existing_type=column["type"],
                existing_nullable=column["nullable"],
                existing_server_default=sa.text(str(default)) if default is not None else None,
                existing_autoincrement=column.get("autoincrement", False),
                existing_comment=column.get("comment"),
                comment=comment,
            )


def upgrade() -> None:
    for table_name, comment in TABLE_COMMENTS.items():
        op.create_table_comment(table_name, comment, existing_comment=None)
    _alter_comments(COLUMN_COMMENTS)


def downgrade() -> None:
    empty_comments = {
        table_name: {column_name: None for column_name in column_comments}
        for table_name, column_comments in COLUMN_COMMENTS.items()
    }
    _alter_comments(empty_comments)
    for table_name, comment in reversed(TABLE_COMMENTS.items()):
        op.drop_table_comment(table_name, existing_comment=comment)
