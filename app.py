"""
Backend Flask — Calculadora de Cashback
=========================================
Banco de dados: PostgreSQL (via variável de ambiente DATABASE_URL)

Local: defina DATABASE_URL no seu .env ou exporte no terminal:
  export DATABASE_URL="postgresql://usuario:senha@localhost:5432/cashback_db"

Render/Railway/Heroku: a variável DATABASE_URL é injetada automaticamente
quando você conecta um banco PostgreSQL ao serviço.
"""

import os
import logging
import psycopg2
import psycopg2.extras
from flask import Flask, request, jsonify, send_from_directory

# ─── Configuração ──────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder="static")
logging.basicConfig(level=logging.INFO)

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "Variável de ambiente DATABASE_URL não definida. "
        "Configure a string de conexão do PostgreSQL antes de iniciar o servidor."
    )

# Render usa "postgres://", mas psycopg2 exige "postgresql://"
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)


# ─── Banco de dados ────────────────────────────────────────────────────────────
def get_db():
    """Retorna uma nova conexão com o PostgreSQL."""
    return psycopg2.connect(DATABASE_URL)


def init_db():
    """Cria a tabela de consultas caso não exista."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS consultas (
                    id                  SERIAL PRIMARY KEY,
                    ip                  TEXT NOT NULL,
                    tipo_cliente        TEXT NOT NULL,
                    valor_produto       NUMERIC(12,2) NOT NULL,
                    percentual_cupom    NUMERIC(5,4) NOT NULL,
                    desconto_cupom      NUMERIC(12,2) NOT NULL,
                    valor_final         NUMERIC(12,2) NOT NULL,
                    cashback_base       NUMERIC(12,2) NOT NULL,
                    bonus_vip           NUMERIC(12,2) NOT NULL,
                    cashback_pre_dobro  NUMERIC(12,2) NOT NULL,
                    dobro_aplicado      BOOLEAN NOT NULL,
                    cashback_final      NUMERIC(12,2) NOT NULL,
                    criado_em           TIMESTAMP DEFAULT NOW()
                )
            """)
        conn.commit()
    logging.info("Tabela 'consultas' verificada/criada com sucesso.")


# ─── Lógica de negócio ────────────────────────────────────────────────────────
def calcular_cashback(valor_produto: float, percentual_cupom: float, eh_vip: bool) -> dict:
    """
    Regras (conforme documentos internos):
      1. Valor final = valor_produto - (valor_produto * percentual_cupom)
      2. Cashback base = valor_final * 5%
      3. Se VIP: bonus = cashback_base * 10%
      4. Se valor_final > R$500: cashback = cashback * 2  (aplicado por último)
    """
    TAXA_BASE = 0.05
    TAXA_BONUS_VIP = 0.10
    LIMITE_DOBRO = 500.0

    desconto_cupom = round(valor_produto * percentual_cupom, 2)
    valor_final = round(valor_produto - desconto_cupom, 2)
    cashback_base = round(valor_final * TAXA_BASE, 2)
    bonus_vip = round(cashback_base * TAXA_BONUS_VIP, 2) if eh_vip else 0.0
    cashback_pre_dobro = round(cashback_base + bonus_vip, 2)
    dobro_aplicado = valor_final > LIMITE_DOBRO
    cashback_final = round(cashback_pre_dobro * 2, 2) if dobro_aplicado else cashback_pre_dobro

    return {
        "desconto_cupom": desconto_cupom,
        "valor_final": valor_final,
        "cashback_base": cashback_base,
        "bonus_vip": bonus_vip,
        "cashback_pre_dobro": cashback_pre_dobro,
        "dobro_aplicado": dobro_aplicado,
        "cashback_final": cashback_final,
    }


# ─── Rotas ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/calcular", methods=["POST"])
def api_calcular():
    """
    Calcula o cashback e persiste o registro vinculado ao IP do cliente.
    Body JSON:
      { "tipo_cliente": "vip"|"regular", "valor_produto": 600.0, "percentual_cupom": 0.20 }
    """
    try:
        data = request.get_json(force=True)
        tipo_cliente = data.get("tipo_cliente", "regular").lower()
        valor_produto = float(data.get("valor_produto", 0))
        percentual_cupom = float(data.get("percentual_cupom", 0))

        if valor_produto <= 0:
            return jsonify({"erro": "Valor do produto deve ser maior que zero."}), 400
        if not (0 <= percentual_cupom < 1):
            return jsonify({"erro": "Percentual do cupom deve estar entre 0 e 0.99."}), 400

        eh_vip = tipo_cliente == "vip"
        resultado = calcular_cashback(valor_produto, percentual_cupom, eh_vip)

        # Captura IP real (considera proxy reverso, padrão em PaaS como Render)
        ip = request.headers.get("X-Forwarded-For", request.remote_addr)
        if ip and "," in ip:
            ip = ip.split(",")[0].strip()

        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO consultas
                        (ip, tipo_cliente, valor_produto, percentual_cupom,
                         desconto_cupom, valor_final, cashback_base, bonus_vip,
                         cashback_pre_dobro, dobro_aplicado, cashback_final)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    ip, tipo_cliente, valor_produto, percentual_cupom,
                    resultado["desconto_cupom"], resultado["valor_final"],
                    resultado["cashback_base"], resultado["bonus_vip"],
                    resultado["cashback_pre_dobro"], resultado["dobro_aplicado"],
                    resultado["cashback_final"]
                ))
            conn.commit()

        return jsonify({
            "tipo_cliente": tipo_cliente,
            "valor_produto": valor_produto,
            "percentual_cupom": percentual_cupom,
            **resultado
        })

    except (ValueError, TypeError) as e:
        return jsonify({"erro": f"Dados inválidos: {str(e)}"}), 400
    except Exception:
        logging.exception("Erro ao calcular cashback")
        return jsonify({"erro": "Erro interno do servidor."}), 500


@app.route("/api/historico", methods=["GET"])
def api_historico():
    """Retorna o histórico de consultas do IP que acessa a rota."""
    try:
        ip = request.headers.get("X-Forwarded-For", request.remote_addr)
        if ip and "," in ip:
            ip = ip.split(",")[0].strip()

        with get_db() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT tipo_cliente, valor_produto, percentual_cupom,
                           desconto_cupom, valor_final, cashback_base, bonus_vip,
                           cashback_pre_dobro, dobro_aplicado, cashback_final,
                           criado_em
                    FROM consultas
                    WHERE ip = %s
                    ORDER BY criado_em DESC
                    LIMIT 50
                """, (ip,))
                rows = cur.fetchall()

        # Converte Decimal/datetime para tipos serializáveis em JSON
        historico = []
        for r in rows:
            item = dict(r)
            item["valor_produto"] = float(item["valor_produto"])
            item["percentual_cupom"] = float(item["percentual_cupom"])
            item["desconto_cupom"] = float(item["desconto_cupom"])
            item["valor_final"] = float(item["valor_final"])
            item["cashback_base"] = float(item["cashback_base"])
            item["bonus_vip"] = float(item["bonus_vip"])
            item["cashback_pre_dobro"] = float(item["cashback_pre_dobro"])
            item["cashback_final"] = float(item["cashback_final"])
            item["criado_em"] = item["criado_em"].isoformat()
            historico.append(item)

        return jsonify({"ip": ip, "total": len(historico), "historico": historico})

    except Exception:
        logging.exception("Erro ao buscar histórico")
        return jsonify({"erro": "Erro interno do servidor."}), 500


# ─── Inicialização ────────────────────────────────────────────────────────────
# init_db() roda no import do módulo, garantindo que a tabela exista
# tanto em `python app.py` (local) quanto sob gunicorn (produção/Render).
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("=" * 50)
    print(f"  Servidor de Cashback iniciado na porta {port}!")
    print("=" * 50)
    app.run(debug=False, host="0.0.0.0", port=port)
