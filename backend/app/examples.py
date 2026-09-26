"""内置示例模型。"""

EXAMPLES = [
    {
        "name": "电商下单（含超时重试与补偿）",
        "spec": {
            "start": "order",
            "variables": [
                {"name": "vip", "type": "bool", "initial": False},
            ],
            "activities": [
                {
                    "id": "order", "name": "创建订单",
                    "effect": "orderCreated", "idempotent": True, "can_fail": False,
                    "post_crash": True,
                    "assign": [{"var": "vip", "expr": "true"}],
                    "comp_can_fail": False,
                },
                {
                    "id": "charge", "name": "调用支付扣款",
                    "effect": "accountCharged", "idempotent": False,
                    "can_fail": True, "retryable": True, "max_attempts": 3,
                    "post_crash": True,
                    "comp_can_fail": False,
                },
                {
                    "id": "coupon", "name": "扣减优惠券",
                    "pre": "vip",
                    "effect": "couponUsed", "idempotent": True,
                    "can_fail": True, "retryable": True, "max_attempts": 2,
                    "comp_can_fail": False,
                },
                {
                    "id": "ship", "name": "通知仓库发货",
                    "effect": "shipNotified", "idempotent": True,
                    "can_fail": True, "retryable": True, "max_attempts": 2,
                    "comp_can_fail": False,
                },
            ],
            "edges": [
                {"src": "order", "dst": "charge"},
                {"src": "charge", "dst": "coupon"},
                {"src": "charge", "dst": "ship", "guard": "not vip"},
                {"src": "coupon", "dst": "ship"},
            ],
            "invariants": {
                "at_most_once": True,
                "undo_on_failure": True,
                "custom": [
                    {"name": "扣款至多生效一次", "expr": 'effects("accountCharged") <= 1'},
                    {"name": "发货前必须已扣款",
                     "expr": 'effects("shipNotified") = 0 or effects("accountCharged") > 0'},
                ],
            },
        },
    },
    {
        "name": "两活动（补偿安全，验证干净结论）",
        "spec": {
            "start": "a",
            "variables": [],
            "activities": [
                {"id": "a", "name": "活动A", "effect": "effA", "idempotent": True,
                 "can_fail": True, "retryable": True, "max_attempts": 2,
                 "post_crash": True, "comp_can_fail": False},
                {"id": "b", "name": "活动B", "effect": "effB", "idempotent": True,
                 "can_fail": True, "retryable": True, "max_attempts": 2,
                 "comp_can_fail": False},
            ],
            "edges": [{"src": "a", "dst": "b"}],
            "invariants": {"at_most_once": True, "undo_on_failure": True, "custom": []},
        },
    },
    {
        "name": "补偿动作本身可能失败",
        "spec": {
            "start": "reserve",
            "variables": [],
            "activities": [
                {"id": "reserve", "name": "预留库存", "effect": "stockReserved",
                 "idempotent": True, "can_fail": True, "retryable": True, "max_attempts": 2,
                 "comp_can_fail": True, "comp_retryable": True, "comp_max_attempts": 2},
                {"id": "confirm", "name": "确认订单", "effect": "orderConfirmed",
                 "idempotent": True, "can_fail": True, "retryable": False, "max_attempts": 1,
                 "comp_can_fail": False},
            ],
            "edges": [{"src": "reserve", "dst": "confirm"}],
            "invariants": {"at_most_once": True, "undo_on_failure": True,
                           "custom": []},
        },
    },
    {
        "name": "并发：双分支写同一账户（数据竞态）",
        "spec": {
            "start": "forkPay",
            "variables": [],
            "activities": [
                {"id": "forkPay", "name": "并行支付编排", "can_fail": False,
                 "retryable": False, "max_attempts": 1, "comp_can_fail": False},
                {"id": "wallet", "name": "钱包扣款", "effect": "walletDebit",
                 "idempotent": True, "can_fail": False, "retryable": False,
                 "max_attempts": 1, "comp_can_fail": False,
                 "writes": ["accountBalance"]},
                {"id": "points", "name": "积分抵扣", "effect": "pointsDebit",
                 "idempotent": True, "can_fail": False, "retryable": False,
                 "max_attempts": 1, "comp_can_fail": False,
                 "reads": ["accountBalance"], "writes": ["pointsBalance"]},
                {"id": "joinPay", "name": "支付汇合", "can_fail": False,
                 "retryable": False, "max_attempts": 1, "comp_can_fail": False,
                 "reads": ["accountBalance"]},
                {"id": "receipt", "name": "出具凭证", "effect": "receiptIssued",
                 "idempotent": True, "can_fail": False, "retryable": False,
                 "max_attempts": 1, "comp_can_fail": False},
            ],
            "edges": [
                {"src": "forkPay", "dst": "wallet"},
                {"src": "forkPay", "dst": "points"},
                {"src": "wallet", "dst": "joinPay"},
                {"src": "points", "dst": "joinPay"},
                {"src": "joinPay", "dst": "receipt"},
            ],
            "invariants": {"at_most_once": True, "undo_on_failure": True,
                           "custom": [
                               {"name": "出凭证前必须已汇合",
                                "expr": 'effects("receiptIssued") = 0 or succ("joinPay") > 0'},
                           ]},
            "concurrency": {
                "resources": [
                    {"name": "accountBalance"},
                    {"name": "pointsBalance"},
                ],
                "locks": [],
                "groups": [{"id": "pay", "fork": "forkPay", "join": "joinPay"}],
            },
        },
    },
    {
        "name": "并发：相反加锁顺序（锁顺序死锁）",
        "spec": {
            "start": "forkTransfer",
            "variables": [],
            "activities": [
                {"id": "forkTransfer", "name": "并行转账编排", "can_fail": False,
                 "retryable": False, "max_attempts": 1, "comp_can_fail": False},
                {"id": "ab", "name": "A→B：先账户锁再记账锁", "effect": "abTransfer",
                 "idempotent": True, "can_fail": False, "retryable": False,
                 "max_attempts": 1, "comp_can_fail": False,
                 "locks": ["accountLock", "ledgerLock"]},
                {"id": "ba", "name": "B→A：先记账锁再账户锁", "effect": "baTransfer",
                 "idempotent": True, "can_fail": False, "retryable": False,
                 "max_attempts": 1, "comp_can_fail": False,
                 "locks": ["ledgerLock", "accountLock"]},
                {"id": "joinTransfer", "name": "转账汇合", "can_fail": False,
                 "retryable": False, "max_attempts": 1, "comp_can_fail": False},
            ],
            "edges": [
                {"src": "forkTransfer", "dst": "ab"},
                {"src": "forkTransfer", "dst": "ba"},
                {"src": "ab", "dst": "joinTransfer"},
                {"src": "ba", "dst": "joinTransfer"},
            ],
            "invariants": {"at_most_once": True, "undo_on_failure": True,
                           "custom": []},
            "concurrency": {
                "resources": [],
                "locks": [{"name": "accountLock"}, {"name": "ledgerLock"}],
                "groups": [{"id": "transfer", "fork": "forkTransfer",
                            "join": "joinTransfer"}],
            },
        },
    },
    {
        "name": "版本对比·基线（支付不重试，失败即补偿）",
        "spec": {
            "start": "charge",
            "variables": [{"name": "retries", "type": "int", "initial": 0}],
            "activities": [
                {
                    "id": "charge", "name": "调用支付扣款（基线：1 次尝试）",
                    "effect": "accountCharged", "idempotent": False,
                    "can_fail": True, "retryable": False, "max_attempts": 1,
                    "comp_can_fail": False,
                },
                {
                    "id": "receipt", "name": "出具凭证",
                    "effect": "receiptIssued", "idempotent": True,
                    "can_fail": False, "retryable": False, "max_attempts": 1,
                    "comp_can_fail": False,
                    "assign": [{"var": "retries", "expr": "0"}],
                },
            ],
            "edges": [{"src": "charge", "dst": "receipt"}],
            "invariants": {
                "at_most_once": True,
                "undo_on_failure": True,
                "custom": [
                    {"name": "出凭证前必须已扣款",
                     "expr": 'effects("receiptIssued") = 0 or effects("accountCharged") > 0'},
                ],
            },
        },
    },
    {
        "name": "版本对比·候选（支付可重试，记录重试次数）",
        "spec": {
            "start": "charge",
            "variables": [{"name": "retries", "type": "int", "initial": 0}],
            "activities": [
                {
                    "id": "charge", "name": "调用支付扣款（候选：最多 3 次尝试）",
                    "effect": "accountCharged", "idempotent": False,
                    "can_fail": True, "retryable": True, "max_attempts": 3,
                    "comp_can_fail": False,
                },
                {
                    "id": "receipt", "name": "出具凭证",
                    "effect": "receiptIssued", "idempotent": True,
                    "can_fail": False, "retryable": False, "max_attempts": 1,
                    "comp_can_fail": False,
                    "assign": [{"var": "retries", "expr": "succ(\"charge\") - 1"}],
                },
            ],
            "edges": [{"src": "charge", "dst": "receipt"}],
            "invariants": {
                "at_most_once": True,
                "undo_on_failure": True,
                "custom": [
                    {"name": "出凭证前必须已扣款",
                     "expr": 'effects("receiptIssued") = 0 or effects("accountCharged") > 0'},
                ],
            },
        },
    },
]
