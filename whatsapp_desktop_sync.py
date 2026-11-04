#!/usr/bin/env python3
"""
Puente de Sincronización en Vivo: Grupo de WhatsApp -> AGR Legal CRM
=====================================================================
Monitorea la base de datos local de WhatsApp Desktop en macOS (ChatStorage.sqlite)
o recibe mensajes de un grupo específico (ej. 'Comando Procesos AGR') y los envía
al motor de comandos del CRM (http://127.0.0.1:8090/api/whatsapp/send).
"""
import os
import json
import sqlite3
import time
import urllib.request

WA_DB_PATH = os.path.expanduser(
    "~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite"
)
CRM_API_URL = "http://127.0.0.1:8090/api/whatsapp/send"
TARGET_GROUP_KEYWORDS = ["comando", "procesos agr", "crm agr", "control procesos"]


def send_to_crm(sender: str, text: str):
    payload = json.dumps({
        "sender": sender,
        "sender_role": "WHATSAPP DESKTOP",
        "text": text
    }).encode("utf-8")
    req = urllib.request.Request(
        CRM_API_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode("utf-8")
    except Exception as e:
        print(f"[WARN] Error enviando orden al CRM: {e}")
        return None


def monitor_whatsapp_group(poll_interval: int = 4):
    if not os.path.exists(WA_DB_PATH):
        print(f"[INFO] Base de datos de WhatsApp Desktop no encontrada en {WA_DB_PATH}")
        return

    conn = sqlite3.connect(f"file:{WA_DB_PATH}?mode=ro", uri=True)
    cur = conn.cursor()
    cur.execute("SELECT MAX(Z_PK) FROM ZWAMESSAGE")
    last_pk = cur.fetchone()[0] or 0
    print(f"[OK] Monitoreando grupo de WhatsApp desde mensaje ID #{last_pk}...")

    while True:
        try:
            cur.execute(
                """
                SELECT m.Z_PK, m.ZTEXT, s.ZPARTNERNAME, m.ZFROMJID
                FROM ZWAMESSAGE m
                LEFT JOIN ZWACHATSESSION s ON m.ZCHATSESSION = s.Z_PK
                WHERE m.Z_PK > ? AND m.ZTEXT IS NOT NULL
                ORDER BY m.Z_PK ASC
                """,
                (last_pk,),
            )
            rows = cur.fetchall()
            for pk, text, group_name, sender_jid in rows:
                last_pk = max(last_pk, pk)
                gname = (group_name or "").lower()
                if any(k in gname for k in TARGET_GROUP_KEYWORDS) or text.strip().lower().startswith(("/crm", "!crm")):
                    clean_cmd = text.strip()
                    for prefix in ("/crm", "!crm"):
                        if clean_cmd.lower().startswith(prefix):
                            clean_cmd = clean_cmd[len(prefix):].strip()
                    print(f"[WA -> CRM] ({group_name}) {clean_cmd}")
                    send_to_crm(sender=f"WA ({group_name or 'Grupo'})", text=clean_cmd)
        except Exception as e:
            print(f"[WARN] Error leyendo ChatStorage.sqlite: {e}")
        time.sleep(poll_interval)


if __name__ == "__main__":
    monitor_whatsapp_group()
