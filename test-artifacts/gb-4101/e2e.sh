#!/bin/bash
# 端到端剧本：开账 → 复核待等水位 → 关账发布通过 → 迟到账单补算 → 其他客户可查
set -u
API=${API:-http://127.0.0.1:19240}
PY=${PY:-python3}
P=${PERIOD:-2026-12}

TOK=$(curl -s -X POST $API/auth/login -H 'Content-Type: application/json' -d '{"email":"operator@chenggang.test","password":"ColdChain!17"}' | $PY -c 'import json,sys;print(json.load(sys.stdin)["token"])')
VTOK=$(curl -s -X POST $API/auth/login -H 'Content-Type: application/json' -d '{"email":"viewer@chenggang.test","password":"ColdChain!17"}' | $PY -c 'import json,sys;print(json.load(sys.stdin)["token"])')

echo "== 1. 建立账期 $P"
curl -s -X POST $API/periods -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' -d "{\"period\":\"$P\"}" >/dev/null

echo "== 2. 入账基线（开账期内，不算迟到）"
ID=$(date +%s%N | cut -c1-9)
curl -s -X POST $API/ingestions -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
  -d "{\"period\":\"$P\",\"bills\":[
    {\"external_ref\":\"E2E-A\",\"invoice_id\":$((ID+1)),\"customer\":\"澄港海产\",\"route\":\"东线\",\"amount\":500,\"temperature\":5,\"status\":\"settled\"},
    {\"external_ref\":\"E2E-B\",\"invoice_id\":$((ID+2)),\"customer\":\"南港鲜食\",\"route\":\"西线\",\"amount\":600,\"temperature\":6,\"status\":\"settled\"},
    {\"external_ref\":\"E2E-C\",\"invoice_id\":$((ID+3)),\"customer\":\"北湾冷运\",\"route\":\"东线\",\"amount\":700,\"temperature\":7,\"status\":\"settled\"}]}" \
  | $PY -c 'import json,sys;d=json.load(sys.stdin);print("ingest_seq",d["ingest_seq"],"late",d["late"])'

echo "== 3. 提交复核，worker 处理后应停在待等水位"
BID=$(curl -s -X POST $API/batches -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
  -d "{\"name\":\"e2e-$RANDOM\",\"sample_id\":1,\"period\":\"$P\",\"candidate_sql\":\"SELECT customer, SUM(amount) AS total FROM invoices WHERE temperature <= 8 GROUP BY customer HAVING SUM(amount) > 100 ORDER BY customer\"}" \
  | $PY -c 'import json,sys;print(json.load(sys.stdin)["id"])')
sleep 3
curl -s $API/batches -H "Authorization: Bearer $TOK" | $PY -c "import json,sys;[print('status',b['status'],'|',b['status_reason']) for b in json.load(sys.stdin) if b['id']==$BID]"

echo "== 4. 关账后批次应发布通过"
curl -s -X POST $API/periods/$P/close -H "Authorization: Bearer $TOK" | $PY -c 'import json,sys;d=json.load(sys.stdin);print("period",d["status"],"state",d["state"],"stable",d["stable"])'
sleep 2
curl -s $API/batches -H "Authorization: Bearer $TOK" | $PY -c "import json,sys;[print('status',b['status']) for b in json.load(sys.stdin) if b['id']==$BID]"

echo "== 5. 迟到账单，仅澄港海产受影响"
curl -s -X POST $API/ingestions -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
  -d "{\"period\":\"$P\",\"bills\":[{\"external_ref\":\"E2E-LATE\",\"invoice_id\":$((ID+4)),\"customer\":\"澄港海产\",\"route\":\"东线\",\"amount\":950,\"temperature\":4,\"status\":\"settled\"}]}" \
  | $PY -c 'import json,sys;d=json.load(sys.stdin);print("late",d["late"],"seq",d["ingest_seq"],"affected",d["affected_customers"],"watermark",d["watermark"]["label"])'
curl -s $API/evidence/$BID -H "Authorization: Bearer $TOK" | $PY -c 'import json,sys;d=json.load(sys.stdin);r=d["result"];print("state",d["state"],"visible",[x[0] for x in r["original"]["rows"]],"pending",[x[0] for x in r["original"]["pending_rows"]])'

echo "== 6. 等 worker 补算，水位稳定"
sleep 6
curl -s $API/periods/$P -H "Authorization: Bearer $TOK" | $PY -c 'import json,sys;d=json.load(sys.stdin);print("state",d["state"],"stable",d["stable"],"ingest",d["ingest_seq"],"stable_seq",d["stable_seq"])'
curl -s $API/evidence/$BID -H "Authorization: Bearer $TOK" | $PY -c 'import json,sys;d=json.load(sys.stdin);print("evidence state",d["state"],"watermark",d["watermark_seq"],"customers",[x[0] for x in d["result"]["original"]["rows"]],"backfill",d["result"].get("backfill"))'

echo "== 7. 按水位复跑核对 + 下载证据"
curl -s $API/evidence/$BID/restore -H "Authorization: Bearer $TOK" | $PY -c 'import json,sys;d=json.load(sys.stdin);print("restore watermark",d["watermark_seq"],"matches_evidence",d["matches_evidence"],"equivalent",d["equivalent"])'
curl -s $API/evidence/$BID/export -H "Authorization: Bearer $TOK" | $PY -c 'import json,sys;d=json.load(sys.stdin);print("export watermark",d["watermark"]["ingest_seq"],"note",d["restore_note"])'

echo "== 8. 观察员只读"
CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST $API/ingestions -H "Authorization: Bearer $VTOK" -H 'Content-Type: application/json' \
  -d "{\"period\":\"$P\",\"bills\":[{\"external_ref\":\"V-1\",\"invoice_id\":$((ID+9)),\"customer\":\"澄港海产\",\"route\":\"东线\",\"amount\":1,\"temperature\":5,\"status\":\"settled\"}]}")
echo "viewer ingest http $CODE"
