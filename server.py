import os
import re
import json
import sqlite3
import shutil
import base64
import urllib.request
import urllib.parse
import ssl
from datetime import datetime
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOCS_AGR_DIR = os.environ.get("DOCS_AGR_DIR", "/Users/agr/Documents/agr")
BUNDLED_DB_PATH = os.path.join(BASE_DIR, "crm_legal_agr.sqlite")
BUNDLED_STORAGE_DIR = os.path.join(BASE_DIR, "storage_expedientes")

FILES_STORAGE_DIR = os.environ.get("FILES_STORAGE_DIR", BUNDLED_STORAGE_DIR)
DB_PATH = os.environ.get("DB_PATH", BUNDLED_DB_PATH)

os.makedirs(BASE_DIR, exist_ok=True)
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
os.makedirs(FILES_STORAGE_DIR, exist_ok=True)

# Si estamos en Render con disco persistente (/data) en el primer arranque, copiar la BD y archivos semilla
if os.path.abspath(DB_PATH) != os.path.abspath(BUNDLED_DB_PATH) and not os.path.exists(DB_PATH) and os.path.exists(BUNDLED_DB_PATH):
    shutil.copy2(BUNDLED_DB_PATH, DB_PATH)

if os.path.abspath(FILES_STORAGE_DIR) != os.path.abspath(BUNDLED_STORAGE_DIR) and os.path.exists(BUNDLED_STORAGE_DIR):
    for item in os.listdir(BUNDLED_STORAGE_DIR):
        s_src = os.path.join(BUNDLED_STORAGE_DIR, item)
        s_dst = os.path.join(FILES_STORAGE_DIR, item)
        if not os.path.exists(s_dst):
            if os.path.isdir(s_src):
                shutil.copytree(s_src, s_dst)
            else:
                shutil.copy2(s_src, s_dst)

app = FastAPI(title="AGR Legal CRM & WhatsApp Command Center")
app.mount("/files", StaticFiles(directory=FILES_STORAGE_DIR), name="files")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def classify_panel(estado, etapa, rol, radicado):
    est = (estado or "").upper()
    et = (etapa or "").upper()
    if "SICECON" in est or "VERIFICACIÓN" in est or "CREACIÓN" in est or len(str(radicado)) < 15:
        return "RADICADO_SICECON"
    if "INACTIVO" in est or "ARCHIV" in et or "PRECLUSI" in et:
        return "ARCHIVADO_INACTIVO"
    if "JUICIO" in et or "ACUSACI" in et or "CASACI" in et or "AUDIENCIA" in et:
        return "AUDIENCIA_JUICIO"
    if "IMPULSO" in et or "CONCILIACI" in et or "QUERELLA" in et:
        return "IMPULSO_PENDIENTE"
    return "INDAGACION_ACTIVA"


def init_db():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS panels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT UNIQUE,
        nombre TEXT,
        color TEXT,
        orden INTEGER
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS processes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        radicado TEXT UNIQUE,
        jurisdiccion TEXT,
        es_denuncia_fiscalia INTEGER DEFAULT 0,
        rol_firma TEXT,
        cliente TEXT,
        denunciante_victima TEXT,
        denunciado_indiciado TEXT,
        despacho TEXT,
        correo_despacho TEXT,
        telefono_direccion_despacho TEXT,
        seccional_unidad TEXT,
        delito_asunto TEXT,
        municipio TEXT,
        estado TEXT,
        etapa TEXT,
        panel_column TEXT,
        abogado_asignado TEXT,
        prioridad TEXT DEFAULT 'MEDIA',
        fecha_presentacion TEXT,
        fecha_asignacion TEXT,
        ultima_actuacion TEXT,
        notas_json TEXT DEFAULT '[]',
        actuaciones_json TEXT DEFAULT '[]',
        updated_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS attachments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        radicado TEXT,
        filename TEXT,
        file_type TEXT,
        saved_rel_path TEXT,
        uploaded_by TEXT,
        caption TEXT,
        created_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        radicado TEXT,
        titulo TEXT,
        responsable TEXT,
        fecha_limite TEXT,
        estado TEXT DEFAULT 'PENDIENTE',
        creado_por TEXT,
        created_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS whatsapp_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sender TEXT,
        sender_role TEXT,
        message_text TEXT,
        attachment_name TEXT,
        attachment_url TEXT,
        attachment_type TEXT,
        is_bot INTEGER DEFAULT 0,
        command_summary TEXT,
        created_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS whatsapp_config (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """)

    # Default panels
    default_panels = [
        ("RADICADO_SICECON", "1. Radicados / SICECON", "#f59e0b", 1),
        ("INDAGACION_ACTIVA", "2. Indagación Activa", "#3b82f6", 2),
        ("IMPULSO_PENDIENTE", "3. Impulso / Conciliación", "#8b5cf6", 3),
        ("AUDIENCIA_JUICIO", "4. En Audiencia / Juicio", "#10b981", 4),
        ("ARCHIVADO_INACTIVO", "5. Archivado / Inactivo", "#64748b", 5),
    ]
    for slug, nombre, color, orden in default_panels:
        cur.execute(
            "INSERT OR IGNORE INTO panels (slug, nombre, color, orden) VALUES (?, ?, ?, ?)",
            (slug, nombre, color, orden)
        )

    # Default WhatsApp config
    cur.execute("INSERT OR IGNORE INTO whatsapp_config (key, value) VALUES ('group_name', 'Grupo Comando Procesos AGR & Abogados')")
    cur.execute("INSERT OR IGNORE INTO whatsapp_config (key, value) VALUES ('authorized_numbers', '+57 320 3430080 (Dr. Andrés Garzón), Equipo Jurídico AGR')")
    cur.execute("INSERT OR IGNORE INTO whatsapp_config (key, value) VALUES ('auto_sync_spoa', 'true')")

    # Check if processes are already seeded
    cur.execute("SELECT COUNT(*) FROM processes")
    count = cur.fetchone()[0]
    if count == 0:
        seed_initial_data(cur)

    conn.commit()
    conn.close()


def seed_initial_data(cur):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Load detailed SPOA actions if available
    spoa_detail_map = {}
    spoa_detail_path = os.path.join(DOCS_AGR_DIR, "Expedientes_Por_Individualizar_SPOA_Detallado.json")
    if os.path.exists(spoa_detail_path):
        try:
            spoa_list = json.load(open(spoa_detail_path))
            for item in spoa_list:
                n = str(item.get("nunc_21", "")).zfill(21)
                spoa_detail_map[n] = item.get("actuaciones", [])
        except Exception:
            pass

    # 1. Load 124 Denuncias Penales y Asignaciones
    den_path = os.path.join(DOCS_AGR_DIR, "Denuncias_Penales_y_Asignaciones_Fiscalia.json")
    seeded_rads = set()
    if os.path.exists(den_path):
        den_list = json.load(open(den_path))
        for r in den_list:
            rad = str(r.get("RADICADO_NUNC", "")).strip()
            if not rad:
                continue
            seeded_rads.add(rad)
            rol = r.get("ROL_FIRMA", "Apoderado de Víctima / Denunciante")
            den_vic = r.get("DENUNCIANTE_O_VICTIMA", "")
            den_ind = r.get("DENUNCIADO_O_INDICIADO", "")
            # Derive main client name based on role
            if "Defensor" in rol or "Investigado" in rol or "Querellado" in rol:
                cliente = den_ind.split("(")[0].strip() if den_ind else den_vic
            else:
                cliente = den_vic.split("(")[0].strip() if den_vic else den_ind

            estado = r.get("ESTADO_PROCESAL", "ACTIVO")
            etapa = r.get("ETAPA_PROCESAL", "INDAGACIÓN")
            if rad == "2026080500671":
                estado = "EN CREACIÓN DE NOTICIA CRIMINAL (SICECON)"
                etapa = "GESTIONADO COMO DENUNCIA - EN PROCESO CREACIÓN NUNC"
                ult = "SICECON (29/10/2026): Su incidente fue gestionado como DENUNCIA y se encuentra en proceso la creación de la noticia criminal, si el incidente tiene más de 5 días hábiles de radicación por favor comuníquese a la línea 122."
                prio = "ALTA"
            else:
                ult = r.get("ULTIMA_ACTUACION_REGISTRADA", "")
                prio = "ALTA" if "ACTIVO" in estado.upper() else "MEDIA"

            panel_col = classify_panel(estado, etapa, rol, rad)
            acts = spoa_detail_map.get(rad, [])
            notas = []
            if r.get("ASUNTO_CORREO_PRESENTACION"):
                notas.append({
                    "autor": "Correo Electrónico",
                    "fecha": r.get("FECHA_PRESENTACION_DENUNCIA") or r.get("FECHA_ASIGNACION_OFICINA") or now_str,
                    "texto": f"Asunto: {r.get('ASUNTO_CORREO_PRESENTACION')}"
                })

            cur.execute("""
                INSERT OR IGNORE INTO processes (
                    radicado, jurisdiccion, es_denuncia_fiscalia, rol_firma, cliente,
                    denunciante_victima, denunciado_indiciado, despacho, correo_despacho,
                    telefono_direccion_despacho, seccional_unidad, delito_asunto, municipio,
                    estado, etapa, panel_column, abogado_asignado, prioridad,
                    fecha_presentacion, fecha_asignacion, ultima_actuacion,
                    notas_json, actuaciones_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                rad, "PENAL", 1, rol, cliente,
                den_vic, den_ind,
                r.get("FISCALIA_DESPACHO_ASIGNADO", ""),
                r.get("CORREO_FISCALIA_CONOCIMIENTO", ""),
                r.get("DIRECCION_Y_TEL_DESPACHO", ""),
                r.get("SECCIONAL_UNIDAD_FISCALIA", ""),
                r.get("DELITO_INVESTIGADO", ""),
                r.get("MUNICIPIO_HECHOS", ""),
                estado, etapa, panel_col,
                "Dr. Andrés Garzón Roa", prio,
                r.get("FECHA_PRESENTACION_DENUNCIA", ""),
                r.get("FECHA_ASIGNACION_OFICINA", ""),
                ult,
                json.dumps(notas, ensure_ascii=False),
                json.dumps(acts, ensure_ascii=False),
                now_str
            ))

    # 2. Load the rest of the 560 processes (Contraloría, Disciplinario, Penal)
    crm_json_path = "/Users/agr/.gemini/antigravity/scratch/legal_crm/enriched_legal_crm_processes.json"
    if os.path.exists(crm_json_path):
        crm_list = json.load(open(crm_json_path))
        for p in crm_list:
            rad_raw = str(p.get("radicado_principal", "")).strip()
            rad_21 = rad_raw[:21] if (rad_raw.isdigit() and len(rad_raw) == 23 and rad_raw.endswith("00")) else rad_raw
            if not rad_raw or rad_raw in seeded_rads or rad_21 in seeded_rads:
                continue
            seeded_rads.add(rad_raw)
            cat = (p.get("categoria") or "PENAL").upper()
            if "CONTRALOR" in cat or "FISCAL" in cat and "PENAL" not in cat:
                jur = "CONTRALORIA"
            elif "DISCIPLIN" in cat or "PROCURADUR" in cat:
                jur = "DISCIPLINARIO"
            else:
                jur = "PENAL"

            cli = p.get("cliente") or "Por individualizar"
            desp = p.get("despacho_autoridad") or ""
            asunto = p.get("asunto_delito") or ""
            ult_act = p.get("ultima_actuacion_asunto") or ""
            f_ult = p.get("fecha_ultima") or ""
            estado = p.get("estado_verificado") or "ACTIVO"
            etapa = "EN TRÁMITE"
            panel_col = classify_panel(estado, etapa, "Representación / Defensa", rad_raw)
            acts = spoa_detail_map.get(rad_21, [])

            cur.execute("""
                INSERT OR IGNORE INTO processes (
                    radicado, jurisdiccion, es_denuncia_fiscalia, rol_firma, cliente,
                    denunciante_victima, denunciado_indiciado, despacho, correo_despacho,
                    telefono_direccion_despacho, seccional_unidad, delito_asunto, municipio,
                    estado, etapa, panel_column, abogado_asignado, prioridad,
                    fecha_presentacion, fecha_asignacion, ultima_actuacion,
                    notas_json, actuaciones_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                rad_raw, jur, 0, "Representación / Defensa", cli,
                "—", cli, desp, "", "", "", asunto, "",
                estado, etapa, panel_col, "Dr. Andrés Garzón Roa", "MEDIA",
                "", f_ult, ult_act,
                "[]", json.dumps(acts, ensure_ascii=False), now_str
            ))

    # 3. Seed files for Juan Pablo Malaver (2026080500671)
    malaver_dir = os.path.join(DOCS_AGR_DIR, "Juan_Pablo_Malaver")
    if os.path.exists(malaver_dir):
        target_sub = os.path.join(FILES_STORAGE_DIR, "2026080500671")
        os.makedirs(target_sub, exist_ok=True)
        for fname in sorted(os.listdir(malaver_dir)):
            src = os.path.join(malaver_dir, fname)
            if os.path.isfile(src):
                dst = os.path.join(target_sub, fname)
                if not os.path.exists(dst):
                    shutil.copy2(src, dst)
                ftype = "image" if fname.lower().endswith((".png", ".jpg", ".jpeg", ".webp")) else "pdf"
                caption = "Formato Oficial Denuncia Virtual FGN # 2026080500671" if "Denuncia_Virtual" in fname else f"Soporte probatorio caso GoldenRock / Empacom: {fname}"
                cur.execute("""
                    INSERT INTO attachments (radicado, filename, file_type, saved_rel_path, uploaded_by, caption, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, ("2026080500671", fname, ftype, f"2026080500671/{fname}", "Juan Pablo Malaver / Sistema FGN", caption, "2026-08-05 10:46"))

    # 4. Seed initial tasks
    initial_tasks = [
        ("2026080500671", "Comunicarse a la Línea 122 o radicar impulso en SICECON para generación de NUNC de 21 dígitos (más de 5 días hábiles desde radicación)", "Dr. Andrés Garzón Roa", "2026-10-30", "PENDIENTE", "Sistema SICECON"),
        ("110016000049202576168", "Seguimiento a Fiscalía 29 Local Bogotá por denuncia de Hurto Agravado contra Johan Sneider Romero Beltrán", "Equipo Jurídico AGR", "2026-11-02", "PENDIENTE", "Dr. Andrés Garzón Roa"),
        ("251266000415202531534", "Impulso ampliación de denuncia Estafa Mayor Cuantía Cajicá (Ligia Elvira Sánchez y Jean René Cristancho)", "Equipo Jurídico AGR", "2026-11-03", "PENDIENTE", "Dr. Andrés Garzón Roa"),
        ("110016000050202652251", "Revisar citación Fiscalía 48 Seccional en querella de Karen Manrique vs. Edgar Guzmán Robles", "Dr. Andrés Garzón Roa", "2026-11-04", "PENDIENTE", "Dr. Andrés Garzón Roa"),
    ]
    for rad, tit, resp, flim, est, cpor in initial_tasks:
        cur.execute("""
            INSERT INTO tasks (radicado, titulo, responsable, fecha_limite, estado, creado_por, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (rad, tit, resp, flim, est, cpor, now_str))

    # 5. Seed initial WhatsApp Group messages showing the bot in action
    initial_wa = [
        ("Sistema Bot AGR", "BOT", "✅ *Centro de Comando WhatsApp AGR conectado.* Se han cargado *561 procesos* (124 denuncias/asignaciones penales + procesos disciplinarios y de Contraloría).\n\nPuedes enviar órdenes en este grupo como:\n• `estado malaver` o `buscar karen manrique`\n• `mover 2026080500671 a indagacion`\n• `crear panel Casos Urgentes Corte`\n• `tarea en malaver: Llamar línea 122 hoy`\n• `nota en 110016000049202576168: Se aportaron videos de caja`\n• O adjuntar fotos, PDFs y documentos indicando el cliente o radicado.", "", "", "", 1, "Inicialización", now_str),
        ("Dr. Andrés Garzón Roa", "SOCIO DIRECTOR", "estado malaver", "", "", "", 0, "", now_str),
        ("Sistema Bot AGR", "BOT", "📌 *EXPEDIENTE # 2026080500671*\n• *Denunciante / Víctima:* Juan Pablo Malaver (C.C. 79.981.801)\n• *Denunciado:* Camilo Andrés Díaz Mendoza (Golden Rocks S.A.S. / Empacom S.A.S.)\n• *Delito:* Estafa Art. 246 C.P. ($200.000.000 COP)\n• *Estado SICECON:* EN CREACIÓN DE NOTICIA CRIMINAL (SICECON)\n• *Panel Actual:* 1. Radicados / SICECON\n• *Archivos en expediente:* 9 documentos/imágenes adjuntos\n• *Última novedad:* Su incidente fue gestionado como DENUNCIA y se encuentra en proceso la creación de la noticia criminal (>5 días hábiles, llamar línea 122).", "Denuncia_Virtual_2026080500671_Juan_Pablo_Malaver.pdf", "/files/2026080500671/Denuncia_Virtual_2026080500671_Juan_Pablo_Malaver.pdf", "pdf", 1, "Consulta expediente 2026080500671", now_str),
    ]
    for s, sr, mt, an, au, at, ib, cs, ca in initial_wa:
        cur.execute("""
            INSERT INTO whatsapp_messages (sender, sender_role, message_text, attachment_name, attachment_url, attachment_type, is_bot, command_summary, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (s, sr, mt, an, au, at, ib, cs, ca))


import unicodedata


def norm_str(s):
    s = str(s or "").lower()
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def search_processes_flexible(cur, query_str, limit=8):
    clean_q = re.sub(
        r"^(buscar|consultar|estado\s+de|estado|como\s+va|ver|el\s+caso\s+de|caso|proceso|procesos\s+de|radicado|expediente|de|en|a|al)\s+",
        "",
        (query_str or "").strip(),
        flags=re.I,
    ).strip()
    if not clean_q:
        return []
    tokens = [t for t in norm_str(clean_q).split() if len(t) >= 2]
    if not tokens:
        return []
    cur.execute("SELECT * FROM processes ORDER BY es_denuncia_fiscalia DESC, id ASC")
    all_rows = [dict(r) for r in cur.fetchall()]
    matches = []
    for r in all_rows:
        hay = norm_str(
            f"{r.get('radicado','')} {r.get('cliente','')} {r.get('denunciante_victima','')} {r.get('denunciado_indiciado','')} {r.get('delito_asunto','')} {r.get('despacho','')}"
        )
        if all(t in hay for t in tokens):
            matches.append(r)
            if len(matches) >= limit:
                break
    return matches


def find_process_by_hint(cur, hint):
    hint = (hint or "").strip()
    if not hint:
        return None
    # 1. Direct radicado match
    m_num = re.search(r"\b(\d{10,23})\b", hint)
    if m_num:
        num = m_num.group(1)
        cur.execute("SELECT * FROM processes WHERE radicado LIKE ? LIMIT 1", (f"%{num}%",))
        row = cur.fetchone()
        if row:
            return dict(row)
    # 2. Accent-insensitive token search
    matches = search_processes_flexible(cur, hint, limit=1)
    return matches[0] if matches else None


def match_panel_slug(cur, panel_hint):
    ph = (panel_hint or "").strip().lower()
    cur.execute("SELECT * FROM panels ORDER BY orden ASC")
    panels = [dict(r) for r in cur.fetchall()]
    for p in panels:
        if ph in p["slug"].lower() or ph in p["nombre"].lower():
            return p
    if any(k in ph for k in ["sicecon", "radicado", "virtual", "1"]):
        return next((p for p in panels if p["slug"] == "RADICADO_SICECON"), panels[0])
    if any(k in ph for k in ["indagacion", "indagación", "activo", "activa", "2"]):
        return next((p for p in panels if p["slug"] == "INDAGACION_ACTIVA"), panels[0])
    if any(k in ph for k in ["impulso", "conciliacion", "conciliación", "pendiente", "3"]):
        return next((p for p in panels if p["slug"] == "IMPULSO_PENDIENTE"), panels[0])
    if any(k in ph for k in ["audiencia", "juicio", "acusacion", "acusación", "4"]):
        return next((p for p in panels if p["slug"] == "AUDIENCIA_JUICIO"), panels[0])
    if any(k in ph for k in ["archivo", "archivado", "inactivo", "5"]):
        return next((p for p in panels if p["slug"] == "ARCHIVADO_INACTIVO"), panels[0])
    return None


def process_whatsapp_command(sender, sender_role, text, attachment_info=None):
    conn = get_db()
    cur = conn.cursor()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    txt = (text or "").strip()
    low = txt.lower()

    # Save incoming user message first
    att_name = attachment_info["filename"] if attachment_info else ""
    att_url = f"/files/{attachment_info['rel_path']}" if attachment_info else ""
    att_type = attachment_info["file_type"] if attachment_info else ""

    cur.execute("""
        INSERT INTO whatsapp_messages (sender, sender_role, message_text, attachment_name, attachment_url, attachment_type, is_bot, command_summary, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 0, '', ?)
    """, (sender, sender_role, txt, att_name, att_url, att_type, now_str))

    bot_reply = ""
    cmd_summary = ""
    updated_radicado = None

    # CASE A: User uploaded a file/photo/document in WhatsApp
    if attachment_info:
        proc = find_process_by_hint(cur, txt)
        if not proc:
            # Default to most recent active or Malaver if no hint given
            proc = find_process_by_hint(cur, "2026080500671")
        rad = proc["radicado"] if proc else "GENERAL"
        updated_radicado = rad
        # Move file into process folder
        target_dir = os.path.join(FILES_STORAGE_DIR, rad)
        os.makedirs(target_dir, exist_ok=True)
        final_path = os.path.join(target_dir, attachment_info["filename"])
        if attachment_info["abs_path"] != final_path:
            shutil.move(attachment_info["abs_path"], final_path)
        rel_path = f"{rad}/{attachment_info['filename']}"
        # Update user message attachment_url
        cur.execute("UPDATE whatsapp_messages SET attachment_url = ? WHERE id = last_insert_rowid()", (f"/files/{rel_path}",))

        caption = txt or f"Archivo enviado por {sender} vía Grupo de WhatsApp"
        cur.execute("""
            INSERT INTO attachments (radicado, filename, file_type, saved_rel_path, uploaded_by, caption, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (rad, attachment_info["filename"], attachment_info["file_type"], rel_path, f"{sender} (WhatsApp)", caption, now_str))

        # Add note to process
        if proc:
            notas = json.loads(proc.get("notas_json") or "[]")
            notas.insert(0, {
                "autor": f"{sender} (WhatsApp)",
                "fecha": now_str,
                "texto": f"📎 Subió archivo '{attachment_info['filename']}': {caption}"
            })
            cur.execute("UPDATE processes SET notas_json = ?, updated_at = ? WHERE radicado = ?", (json.dumps(notas, ensure_ascii=False), now_str, rad))

        cmd_summary = f"Archivo adjuntado a {rad}"
        bot_reply = (
            f"✅ *Archivo guardado e indexado en el CRM*\n"
            f"• *Archivo:* `{attachment_info['filename']}` ({attachment_info['file_type'].upper()})\n"
            f"• *Expediente vinculado:* `{rad}` — {proc['cliente'] if proc else 'Repositorio General'}\n"
            f"• *Cargado por:* {sender}\n"
            f"• *Nota registrada:* {caption}\n"
            f"🔗 Ya está visible en la pestaña *Documentos y Fotos* del expediente."
        )

    # CASE B: Create a new panel on the Kanban board
    elif re.match(r"^(crear|nuevo|agregar)\s+panel\s+(.+)$", low):
        m = re.match(r"^(?:crear|nuevo|agregar)\s+panel\s+(.+)$", txt, flags=re.I)
        panel_name = m.group(1).strip()
        slug = re.sub(r"[^A-Z0-9_]", "_", panel_name.upper())
        cur.execute("SELECT COALESCE(MAX(orden), 5) + 1 FROM panels")
        next_ord = cur.fetchone()[0]
        colors = ["#ec4899", "#06b6d4", "#eab308", "#14b8a6", "#f97316"]
        color = colors[next_ord % len(colors)]
        cur.execute("INSERT OR IGNORE INTO panels (slug, nombre, color, orden) VALUES (?, ?, ?, ?)", (slug, f"{next_ord}. {panel_name}", color, next_ord))
        cmd_summary = f"Panel creado: {panel_name}"
        bot_reply = f"📊 *Nuevo Panel Creado en el Tablero CRM*\n• *Nombre:* `{next_ord}. {panel_name}`\n• *Código:* `{slug}`\nYa puedes mover procesos a este panel diciendo: `mover <radicado o cliente> a {panel_name}`."

    # CASE C: Rename or delete a panel
    elif re.match(r"^eliminar\s+panel\s+(.+)$", low):
        m = re.match(r"^eliminar\s+panel\s+(.+)$", txt, flags=re.I)
        p_obj = match_panel_slug(cur, m.group(1))
        if p_obj and p_obj["orden"] > 5:
            cur.execute("UPDATE processes SET panel_column = 'INDAGACION_ACTIVA' WHERE panel_column = ?", (p_obj["slug"],))
            cur.execute("DELETE FROM panels WHERE slug = ?", (p_obj["slug"],))
            cmd_summary = f"Panel eliminado: {p_obj['nombre']}"
            bot_reply = f"🗑️ *Panel eliminado:* `{p_obj['nombre']}`. Sus procesos fueron reubicados en *Indagación Activa*."
        else:
            bot_reply = "⚠️ Solo se pueden eliminar paneles personalizados adicionales (los 5 paneles base del flujo penal se mantienen fijos)."

    # CASE D: Move a process to another panel ("mover <caso> a <panel>" / "pasar <caso> a <panel>")
    elif re.match(r"^(mover|pasar|trasladar|ubicar)\s+(.+?)\s+(?:a|al\s+panel|hacia)\s+(.+)$", low):
        m = re.match(r"^(?:mover|pasar|trasladar|ubicar)\s+(.+?)\s+(?:a|al\s+panel|hacia)\s+(.+)$", txt, flags=re.I)
        case_hint, panel_hint = m.group(1).strip(), m.group(2).strip()
        proc = find_process_by_hint(cur, case_hint)
        p_obj = match_panel_slug(cur, panel_hint)
        if not proc:
            bot_reply = f"❌ No encontré ningún proceso que coincida con `{case_hint}`. Intenta con el número de radicado o apellido del cliente."
        elif not p_obj:
            bot_reply = f"❌ No encontré el panel `{panel_hint}`. Si deseas crearlo, escribe: `crear panel {panel_hint}`."
        else:
            rad = proc["radicado"]
            updated_radicado = rad
            notas = json.loads(proc.get("notas_json") or "[]")
            notas.insert(0, {
                "autor": f"{sender} (WhatsApp)",
                "fecha": now_str,
                "texto": f"🔄 Movió el expediente al panel '{p_obj['nombre']}'"
            })
            cur.execute(
                "UPDATE processes SET panel_column = ?, notas_json = ?, updated_at = ? WHERE radicado = ?",
                (p_obj["slug"], json.dumps(notas, ensure_ascii=False), now_str, rad)
            )
            cmd_summary = f"Movido {rad} -> {p_obj['nombre']}"
            bot_reply = (
                f"✅ *Panel Actualizado en Tiempo Real*\n"
                f"• *Expediente:* `{rad}` ({proc['cliente']})\n"
                f"• *Nuevo Panel:* *{p_obj['nombre']}*\n"
                f"• *Ordenado por:* {sender}"
            )

    # CASE E: Change status or priority ("cambiar estado de <caso> a <nuevo_estado>" / "prioridad <alta/media/baja> para <caso>")
    elif re.match(r"^(?:cambiar\s+estado\s+de|estado\s+de)\s+(.+?)\s+a\s+(.+)$", low):
        m = re.match(r"^(?:cambiar\s+estado\s+de|estado\s+de)\s+(.+?)\s+a\s+(.+)$", txt, flags=re.I)
        case_hint, new_status = m.group(1).strip(), m.group(2).strip().upper()
        proc = find_process_by_hint(cur, case_hint)
        if not proc:
            bot_reply = f"❌ No encontré ningún proceso que coincida con `{case_hint}`."
        else:
            rad = proc["radicado"]
            updated_radicado = rad
            new_panel = classify_panel(new_status, proc.get("etapa", ""), proc.get("rol_firma", ""), rad)
            notas = json.loads(proc.get("notas_json") or "[]")
            notas.insert(0, {
                "autor": f"{sender} (WhatsApp)",
                "fecha": now_str,
                "texto": f"⚡ Cambió ESTADO PROCESAL a '{new_status}'"
            })
            cur.execute(
                "UPDATE processes SET estado = ?, panel_column = ?, notas_json = ?, updated_at = ? WHERE radicado = ?",
                (new_status, new_panel, json.dumps(notas, ensure_ascii=False), now_str, rad)
            )
            cmd_summary = f"Estado {rad} -> {new_status}"
            bot_reply = (
                f"✅ *Estado Procesal Modificado*\n"
                f"• *Expediente:* `{rad}` ({proc['cliente']})\n"
                f"• *Nuevo Estado:* `{new_status}`\n"
                f"• *Actualizado por:* {sender}"
            )

    elif re.match(r"^prioridad\s+(alta|media|baja)\s+(?:en|para)\s+(.+)$", low):
        m = re.match(r"^prioridad\s+(alta|media|baja)\s+(?:en|para)\s+(.+)$", txt, flags=re.I)
        prio, case_hint = m.group(1).upper(), m.group(2).strip()
        proc = find_process_by_hint(cur, case_hint)
        if not proc:
            bot_reply = f"❌ No encontré ningún proceso que coincida con `{case_hint}`."
        else:
            rad = proc["radicado"]
            updated_radicado = rad
            cur.execute("UPDATE processes SET prioridad = ?, updated_at = ? WHERE radicado = ?", (prio, now_str, rad))
            cmd_summary = f"Prioridad {rad} -> {prio}"
            bot_reply = f"🚨 *Prioridad actualizada a {prio}* en el expediente `{rad}` ({proc['cliente']})."

    # CASE F: Assign lawyer ("asignar <caso> a <abogado>")
    elif re.match(r"^asignar\s+(.+?)\s+a\s+(.+)$", low):
        m = re.match(r"^asignar\s+(.+?)\s+a\s+(.+)$", txt, flags=re.I)
        case_hint, lawyer = m.group(1).strip(), m.group(2).strip()
        proc = find_process_by_hint(cur, case_hint)
        if not proc:
            bot_reply = f"❌ No encontré ningún proceso que coincida con `{case_hint}`."
        else:
            rad = proc["radicado"]
            updated_radicado = rad
            notas = json.loads(proc.get("notas_json") or "[]")
            notas.insert(0, {
                "autor": f"{sender} (WhatsApp)",
                "fecha": now_str,
                "texto": f"👤 Asignó el expediente al abogado: {lawyer}"
            })
            cur.execute(
                "UPDATE processes SET abogado_asignado = ?, notas_json = ?, updated_at = ? WHERE radicado = ?",
                (lawyer, json.dumps(notas, ensure_ascii=False), now_str, rad)
            )
            cmd_summary = f"Asignado {rad} -> {lawyer}"
            bot_reply = (
                f"👤 *Abogado Responsable Actualizado*\n"
                f"• *Expediente:* `{rad}` ({proc['cliente']})\n"
                f"• *Nuevo Responsable:* *{lawyer}*"
            )

    # CASE G: Add a task ("tarea en <caso>: <descripcion>")
    elif re.match(r"^tarea\s+(?:en|para)\s+(.+?)\s*:\s*(.+)$", low):
        m = re.match(r"^tarea\s+(?:en|para)\s+(.+?)\s*:\s*(.+)$", txt, flags=re.I)
        case_hint, task_desc = m.group(1).strip(), m.group(2).strip()
        proc = find_process_by_hint(cur, case_hint)
        rad = proc["radicado"] if proc else "GENERAL"
        updated_radicado = rad
        resp_lawyer = proc["abogado_asignado"] if proc else "Equipo Jurídico AGR"
        cur.execute("""
            INSERT INTO tasks (radicado, titulo, responsable, fecha_limite, estado, creado_por, created_at)
            VALUES (?, ?, ?, ?, 'PENDIENTE', ?, ?)
        """, (rad, task_desc, resp_lawyer, datetime.now().strftime("%Y-%m-%d"), f"{sender} (WhatsApp)", now_str))
        cmd_summary = f"Nueva tarea en {rad}"
        bot_reply = (
            f"📌 *Nueva Tarea Creada desde WhatsApp*\n"
            f"• *Expediente:* `{rad}` ({proc['cliente'] if proc else 'General'})\n"
            f"• *Tarea:* {task_desc}\n"
            f"• *Responsable:* {resp_lawyer}"
        )

    # CASE H: Add a note / bitácora ("nota en <caso>: <texto>")
    elif re.match(r"^(?:nota|bitacora|bitácora|observacion|observación)\s+(?:en|para|de)\s+(.+?)\s*:\s*(.+)$", low):
        m = re.match(r"^(?:nota|bitacora|bitácora|observacion|observación)\s+(?:en|para|de)\s+(.+?)\s*:\s*(.+)$", txt, flags=re.I)
        case_hint, note_txt = m.group(1).strip(), m.group(2).strip()
        proc = find_process_by_hint(cur, case_hint)
        if not proc:
            bot_reply = f"❌ No encontré ningún proceso que coincida con `{case_hint}`."
        else:
            rad = proc["radicado"]
            updated_radicado = rad
            notas = json.loads(proc.get("notas_json") or "[]")
            notas.insert(0, {
                "autor": f"{sender} (WhatsApp)",
                "fecha": now_str,
                "texto": note_txt
            })
            cur.execute(
                "UPDATE processes SET notas_json = ?, ultima_actuacion = ?, updated_at = ? WHERE radicado = ?",
                (json.dumps(notas, ensure_ascii=False), f"[{now_str}] {note_txt}", now_str, rad)
            )
            cmd_summary = f"Nota agregada en {rad}"
            bot_reply = (
                f"📝 *Nota Registrada en el Expediente*\n"
                f"• *Expediente:* `{rad}` ({proc['cliente']})\n"
                f"• *Anotación:* {note_txt}\n"
                f"• *Autor:* {sender}"
            )

    # CASE I: Query status / search ("estado <caso>", "buscar <caso>", "procesos de <cliente>", or just a name/radicado)
    else:
        q_clean = re.sub(r"^(buscar|consultar|estado\s+de|estado|como\s+va|ver|proceso|procesos\s+de|expediente)\s+", "", txt, flags=re.I).strip()
        rows = search_processes_flexible(cur, q_clean, limit=8)
        if len(rows) == 1:
            p = rows[0]
            rad = p["radicado"]
            updated_radicado = rad
            cur.execute("SELECT COUNT(*) FROM attachments WHERE radicado = ?", (rad,))
            att_cnt = cur.fetchone()[0]
            cur.execute("SELECT nombre FROM panels WHERE slug = ?", (p["panel_column"],))
            prow = cur.fetchone()
            pname = prow[0] if prow else p["panel_column"]
            cmd_summary = f"Consulta {rad}"
            bot_reply = (
                f"📌 *EXPEDIENTE `{rad}` ({p['jurisdiccion']})*\n"
                f"• *Rol Firma:* {p['rol_firma']}\n"
                f"• *Denunciante / Víctima:* {p['denunciante_victima']}\n"
                f"• *Denunciado / Indiciado:* {p['denunciado_indiciado']}\n"
                f"• *Despacho:* {p['despacho'] or 'Por asignar'} {('(' + p['correo_despacho'] + ')') if p['correo_despacho'] else ''}\n"
                f"• *Delito / Asunto:* {p['delito_asunto']}\n"
                f"• *Estado:* *{p['estado']}* | *Panel:* `{pname}`\n"
                f"• *Responsable:* {p['abogado_asignado']} | *Prioridad:* `{p['prioridad']}`\n"
                f"• *Archivos adjuntos:* {att_cnt} documento(s)\n"
                f"• *Última actuación:* {p['ultima_actuacion'] or 'Sin actuaciones recientes'}"
            )
        elif len(rows) > 1:
            lines = [f"🔍 *Encontré {len(rows)} procesos para `{q_clean}`:*"]
            for idx, r in enumerate(rows, 1):
                lines.append(f"{idx}. `{r['radicado']}` — *{r['cliente']}* | {r['despacho'] or r['jurisdiccion']} | *{r['estado']}*")
            lines.append("\n💡 Escribe el número de radicado para ver la ficha completa o dar una orden sobre él.")
            cmd_summary = f"Búsqueda: {q_clean} ({len(rows)} resultados)"
            bot_reply = "\n".join(lines)
        else:
            cmd_summary = "Ayuda de comandos"
            bot_reply = (
                f"🤖 No encontré un proceso con `{txt}`.\n\n"
                f"*Comandos disponibles en el grupo de WhatsApp:*\n"
                f"1️⃣ *Consultar:* `estado malaver` o `buscar karen manrique` o `110016000049202576168`\n"
                f"2️⃣ *Mover de panel:* `mover 2026080500671 a indagacion` o `mover malaver a audiencia`\n"
                f"3️⃣ *Cambiar estado/prioridad:* `cambiar estado de malaver a ACTIVO` o `prioridad alta para malaver`\n"
                f"4️⃣ *Crear panel:* `crear panel Casos Prioritarios CSJ`\n"
                f"5️⃣ *Asignar abogado:* `asignar malaver a Jesús López`\n"
                f"6️⃣ *Crear tarea o nota:* `tarea en malaver: Llamar línea 122` o `nota en malaver: Se radicó impulso`\n"
                f"7️⃣ *Subir archivos/fotos:* Adjunta cualquier PDF, foto o Word con el nombre del cliente o radicado."
            )

    # Save bot reply
    cur.execute("""
        INSERT INTO whatsapp_messages (sender, sender_role, message_text, attachment_name, attachment_url, attachment_type, is_bot, command_summary, created_at)
        VALUES ('Bot Controlador AGR', 'BOT', ?, '', '', '', 1, ?, ?)
    """, (bot_reply, cmd_summary, now_str))

    conn.commit()
    conn.close()
    return {
        "ok": True,
        "bot_reply": bot_reply,
        "command_summary": cmd_summary,
        "updated_radicado": updated_radicado
    }


@app.get("/api/bootstrap")
def api_bootstrap():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM panels ORDER BY orden ASC")
    panels = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT * FROM processes ORDER BY es_denuncia_fiscalia DESC, prioridad ASC, id ASC")
    processes = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT * FROM attachments ORDER BY id DESC")
    attachments = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT * FROM tasks ORDER BY estado DESC, id DESC")
    tasks = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT * FROM whatsapp_messages ORDER BY id ASC LIMIT 200")
    wa_messages = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT key, value FROM whatsapp_config")
    wa_config = {r["key"]: r["value"] for r in cur.fetchall()}

    conn.close()
    return JSONResponse({
        "panels": panels,
        "processes": processes,
        "attachments": attachments,
        "tasks": tasks,
        "whatsapp_messages": wa_messages,
        "whatsapp_config": wa_config
    })


@app.get("/api/process/{radicado}")
def api_get_process(radicado: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM processes WHERE radicado = ?", (radicado,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return JSONResponse({"error": "No encontrado"}, status_code=404)
    proc = dict(row)
    cur.execute("SELECT * FROM attachments WHERE radicado = ? ORDER BY id DESC", (radicado,))
    proc["attachments"] = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT * FROM tasks WHERE radicado = ? ORDER BY id DESC", (radicado,))
    proc["tasks"] = [dict(r) for r in cur.fetchall()]
    conn.close()
    return JSONResponse(proc)


@app.post("/api/process/{radicado}/update")
async def api_update_process(radicado: str, req: Request):
    body = await req.json()
    conn = get_db()
    cur = conn.cursor()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    fields = []
    vals = []
    for k in ["cliente", "denunciante_victima", "denunciado_indiciado", "despacho", "correo_despacho", "delito_asunto", "estado", "etapa", "panel_column", "abogado_asignado", "prioridad", "ultima_actuacion"]:
        if k in body:
            fields.append(f"{k} = ?")
            vals.append(body[k])
    if "new_note" in body and body["new_note"].strip():
        cur.execute("SELECT notas_json FROM processes WHERE radicado = ?", (radicado,))
        r = cur.fetchone()
        notas = json.loads((r["notas_json"] if r else "[]") or "[]")
        notas.insert(0, {
            "autor": body.get("autor", "Dr. Andrés Garzón Roa (Web)"),
            "fecha": now_str,
            "texto": body["new_note"].strip()
        })
        fields.append("notas_json = ?")
        vals.append(json.dumps(notas, ensure_ascii=False))

    if fields:
        fields.append("updated_at = ?")
        vals.append(now_str)
        vals.append(radicado)
        cur.execute(f"UPDATE processes SET {', '.join(fields)} WHERE radicado = ?", tuple(vals))
        conn.commit()

    conn.close()
    return JSONResponse({"ok": True})


@app.post("/api/whatsapp/send")
async def api_whatsapp_send(req: Request):
    body = await req.json()
    sender = body.get("sender", "Dr. Andrés Garzón Roa")
    sender_role = body.get("sender_role", "SOCIO DIRECTOR")
    text = body.get("text", "")
    file_data = body.get("file_data")  # optional: {filename, base64, content_type}

    att_info = None
    if file_data and file_data.get("base64"):
        raw_bytes = base64.b64decode(file_data["base64"].split(",")[-1])
        safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", file_data.get("filename", "archivo_whatsapp.bin"))
        tmp_dir = os.path.join(FILES_STORAGE_DIR, "tmp")
        os.makedirs(tmp_dir, exist_ok=True)
        abs_p = os.path.join(tmp_dir, f"{int(datetime.now().timestamp())}_{safe_name}")
        with open(abs_p, "wb") as f:
            f.write(raw_bytes)
        ext = safe_name.lower().split(".")[-1]
        ftype = "image" if ext in ["png", "jpg", "jpeg", "gif", "webp"] else ("pdf" if ext == "pdf" else "doc")
        att_info = {
            "filename": safe_name,
            "abs_path": abs_p,
            "rel_path": f"tmp/{os.path.basename(abs_p)}",
            "file_type": ftype
        }

    res = process_whatsapp_command(sender, sender_role, text, att_info)
    return JSONResponse(res)


@app.post("/api/whatsapp/webhook")
async def api_whatsapp_webhook(req: Request):
    """
    Webhook universal 24/7 en la nube para Grupos de WhatsApp
    Compatible con Evolution API, Green-API, Whapi.Cloud y Meta Cloud API.
    """
    body = await req.json()
    sender = (
        body.get("senderName")
        or body.get("pushName")
        or body.get("sender")
        or "Miembro Grupo WhatsApp"
    )
    text = (
        body.get("textMessage")
        or body.get("caption")
        or body.get("text")
        or (body.get("message", {}) or {}).get("conversation", "")
        or ""
    )
    if not text and not body.get("file_data"):
        return JSONResponse({"ok": True, "ignored": True})

    res = process_whatsapp_command(sender, "GRUPO WHATSAPP 24/7", text, None)
    return JSONResponse(res)


@app.post("/api/tasks/toggle/{task_id}")
def api_toggle_task(task_id: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT estado FROM tasks WHERE id = ?", (task_id,))
    r = cur.fetchone()
    if r:
        new_est = "CUMPLIDA" if r["estado"] != "CUMPLIDA" else "PENDIENTE"
        cur.execute("UPDATE tasks SET estado = ? WHERE id = ?", (new_est, task_id))
        conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


@app.post("/api/panels/create")
async def api_create_panel(req: Request):
    body = await req.json()
    nombre = body.get("nombre", "").strip()
    if not nombre:
        return JSONResponse({"error": "Nombre vacío"}, status_code=400)
    res = process_whatsapp_command("Dr. Andrés Garzón Roa (Web)", "SOCIO DIRECTOR", f"crear panel {nombre}")
    return JSONResponse(res)


@app.get("/", response_class=HTMLResponse)
def serve_index():
    index_path = os.path.join(BASE_DIR, "index.html")
    with open(index_path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


init_db()
