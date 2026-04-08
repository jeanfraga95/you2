
#!/bin/bash
# ================================================================
#  install.sh — Instalador YouTube Stream Manager v2.0
#  Testado: Ubuntu 20.04 / 22.04 / 24.04
#  Uso: bash install.sh
# ================================================================

set -e
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

ok()   { echo -e "${GREEN}✓${NC} $1"; }
warn() { echo -e "${YELLOW}⚠${NC} $1"; }
info() { echo -e "${CYAN}→${NC} $1"; }
die()  { echo -e "${RED}✗ ERRO:${NC} $1"; exit 1; }

echo -e "${BOLD}"
echo "╔══════════════════════════════════════════╗"
echo "║  YouTube Stream Manager — Instalador    ║"
echo "╚══════════════════════════════════════════╝"
echo -e "${NC}"

# ── Verifica root ───────────────────────────────────────────────
[[ $EUID -ne 0 ]] && die "Execute como root: sudo bash install.sh"

# ── Diretório do script ─────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$SCRIPT_DIR/app.py"
[[ -f "$APP" ]] || die "app.py não encontrado em $SCRIPT_DIR"

# ────────────────────────────────────────────────────────────────
# 1. SISTEMA
# ────────────────────────────────────────────────────────────────
info "Atualizando lista de pacotes…"
apt-get update -qq

info "Instalando dependências do sistema…"
apt-get install -y -qq \
  python3 python3-pip python3-venv \
  curl wget screen ffmpeg \
  2>/dev/null || warn "Alguns pacotes já estavam instalados."

ok "Pacotes do sistema OK"

# ────────────────────────────────────────────────────────────────
# 2. PYTHON VERSION CHECK
# ────────────────────────────────────────────────────────────────
PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)

info "Python detectado: $PY_VER"

if [[ $PY_MAJOR -lt 3 || ($PY_MAJOR -eq 3 && $PY_MINOR -lt 8) ]]; then
  warn "Python $PY_VER < 3.8. Instalando Python 3.11…"
  apt-get install -y -qq python3.11 python3.11-venv python3.11-distutils 2>/dev/null || true
  update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1 2>/dev/null || true
  PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
  ok "Python atualizado para $PY_VER"
else
  ok "Python $PY_VER compatível"
fi

# ────────────────────────────────────────────────────────────────
# 3. VENV
# ────────────────────────────────────────────────────────────────
VENV="$SCRIPT_DIR/venv"
info "Configurando ambiente virtual em $VENV…"

if [[ -d "$VENV" ]]; then
  warn "venv já existe — recriando para garantir versões corretas."
  rm -rf "$VENV"
fi

python3 -m venv "$VENV"
ok "venv criado"

PIP="$VENV/bin/pip"
PYTHON="$VENV/bin/python"

info "Atualizando pip…"
"$PIP" install --quiet --upgrade pip

# ────────────────────────────────────────────────────────────────
# 4. DEPENDÊNCIAS PYTHON
# ────────────────────────────────────────────────────────────────
info "Instalando Flask e yt-dlp…"

# Remove versões antigas se existirem
"$PIP" uninstall -y yt-dlp flask 2>/dev/null || true

"$PIP" install --quiet \
  "flask>=2.3,<4.0" \
  "yt-dlp>=2024.1.1" \
  "requests>=2.31"

# Verifica instalação
"$PYTHON" -c "import flask; print(f'Flask {flask.__version__}')" 2>/dev/null \
  && ok "Flask instalado" || die "Falha ao instalar Flask"

"$PYTHON" -c "import yt_dlp; print(f'yt-dlp {yt_dlp.version.__version__}')" 2>/dev/null \
  && ok "yt-dlp instalado" || die "Falha ao instalar yt-dlp"

# ────────────────────────────────────────────────────────────────
# 5. PERMISSÕES
# ────────────────────────────────────────────────────────────────
info "Ajustando permissões…"
touch "$SCRIPT_DIR/channels.json" 2>/dev/null || true
chmod 644 "$SCRIPT_DIR/channels.json" 2>/dev/null || true
chmod +x "$APP"

# ────────────────────────────────────────────────────────────────
# 6. SCRIPT DE START/STOP
# ────────────────────────────────────────────────────────────────
cat > "$SCRIPT_DIR/start.sh" << EOF
#!/bin/bash
# Inicia o servidor em uma sessão screen
SCRIPT_DIR="\$(cd "\$(dirname "\${BASH_SOURCE[0]}")" && pwd)"

# Mata sessão anterior se existir
screen -S ytstream -X quit 2>/dev/null || true
sleep 1

# Inicia nova sessão
screen -dmS ytstream bash -c "
  source \$SCRIPT_DIR/venv/bin/activate
  cd \$SCRIPT_DIR
  python app.py 2>&1 | tee -a ytstream.log
"

sleep 2
if screen -list | grep -q ytstream; then
  echo "✓ Servidor iniciado na sessão screen 'ytstream'"
  echo "  Para ver logs: screen -r ytstream"
  echo "  Para sair dos logs: Ctrl+A depois D"
  IP=\$(curl -s ifconfig.me 2>/dev/null || echo "SEU_IP")
  echo ""
  echo "  Painel: http://\$IP:8010/panel"
  echo "  Stream: http://\$IP:8010/VIDEO_ID"
else
  echo "✗ Falha ao iniciar. Tente: bash start.sh"
fi
EOF

cat > "$SCRIPT_DIR/stop.sh" << EOF
#!/bin/bash
screen -S ytstream -X quit 2>/dev/null && echo "✓ Servidor parado" || echo "Servidor não estava rodando"
EOF

cat > "$SCRIPT_DIR/restart.sh" << EOF
#!/bin/bash
bash "\$(dirname "\$0")/stop.sh"
sleep 2
bash "\$(dirname "\$0")/start.sh"
EOF

cat > "$SCRIPT_DIR/logs.sh" << EOF
#!/bin/bash
tail -f "\$(dirname "\$0")/ytstream.log"
EOF

chmod +x "$SCRIPT_DIR/start.sh" "$SCRIPT_DIR/stop.sh" "$SCRIPT_DIR/restart.sh" "$SCRIPT_DIR/logs.sh"
ok "Scripts start/stop/restart/logs criados"

# ────────────────────────────────────────────────────────────────
# 7. FIREWALL (opcional)
# ────────────────────────────────────────────────────────────────
if command -v ufw &>/dev/null; then
  UFW_STATUS=$(ufw status | head -1)
  if echo "$UFW_STATUS" | grep -q "active"; then
    info "UFW ativo — abrindo porta 8010…"
    ufw allow 8010/tcp >/dev/null 2>&1 && ok "Porta 8010 liberada no UFW"
  fi
fi

# ────────────────────────────────────────────────────────────────
# 8. INICIA AGORA
# ────────────────────────────────────────────────────────────────
info "Iniciando servidor…"
bash "$SCRIPT_DIR/start.sh"

# ────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}══ Instalação concluída! ══${NC}"
echo ""
IP=$(curl -s ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')
echo -e "  ${BOLD}Painel Web:${NC}  http://$IP:8010/panel"
echo -e "  ${BOLD}Status:${NC}      http://$IP:8010/status"
echo -e "  ${BOLD}Stream VLC:${NC}  http://$IP:8010/VIDEO_ID"
echo ""
echo -e "  ${BOLD}Gerenciar:${NC}"
echo "    bash start.sh    — iniciar"
echo "    bash stop.sh     — parar"
echo "    bash restart.sh  — reiniciar"
echo "    bash logs.sh     — ver logs ao vivo"
echo ""
echo -e "  ${YELLOW}Lembre de trocar a senha em app.py:${NC}"
echo "    PANEL_PASSWORD = \"admin123\"  ← linha ~18"
echo ""
