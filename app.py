import os
import logging
import psycopg2
import psycopg2.extras
from datetime import timedelta, timezone
from flask import Flask, request, jsonify, send_from_directory

FUSO_BRASIL = timezone(timedelta(hours=-3))

# Configuração
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


# Banco de dados
def get_db():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS consultas (
                    id                  SERIAL PRIMARY KEY,
                    ip                  TEXT NOT NULL,
                    tipo_cliente        TEXT NOT NULL,
                    valor_compra        NUMERIC(12,2) NOT NULL,
                    desconto_percentual NUMERIC(5,2) NOT NULL,
                    desconto_valor      NUMERIC(12,2) NOT NULL,
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


# Lógica de negócio
def calcular_cashback(valor_compra, desconto_percentual, vip):
    valor_final = valor_compra * (1 - desconto_percentual / 100)
    desconto_valor = valor_compra - valor_final

    cashback_base = valor_final * 0.05

    bonus_vip = 0
    if vip:
        bonus_vip = cashback_base * 0.10

    cashback_pre_dobro = cashback_base + bonus_vip

    dobro_aplicado = valor_final > 500
    cashback_final = cashback_pre_dobro * 2 if dobro_aplicado else cashback_pre_dobro

    return {
        "desconto_valor": round(desconto_valor, 2),
        "valor_final": round(valor_final, 2),
        "cashback_base": round(cashback_base, 2),
        "bonus_vip": round(bonus_vip, 2),
        "cashback_pre_dobro": round(cashback_pre_dobro, 2),
        "dobro_aplicado": dobro_aplicado,
        "cashback_final": round(cashback_final, 2),
    }


# Rotas
@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/calcular", methods=["POST"])
def api_calcular():
    try:
        data = request.get_json(force=True)
        tipo_cliente = data.get("tipo_cliente", "regular").lower()
        valor_compra = float(data.get("valor_compra", 0))
        desconto_percentual = float(data.get("desconto_percentual", 0))

        if valor_compra <= 0:
            return jsonify({"erro": "Valor da compra deve ser maior que zero."}), 400
        if not (0 <= desconto_percentual < 100):
            return jsonify({"erro": "Desconto deve estar entre 0 e 99."}), 400

        vip = tipo_cliente == "vip"
        resultado = calcular_cashback(valor_compra, desconto_percentual, vip)

        ip = request.headers.get("X-Forwarded-For", request.remote_addr)
        if ip and "," in ip:
            ip = ip.split(",")[0].strip()

        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO consultas
                        (ip, tipo_cliente, valor_compra, desconto_percentual,
                         desconto_valor, valor_final, cashback_base, bonus_vip,
                         cashback_pre_dobro, dobro_aplicado, cashback_final)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    ip, tipo_cliente, valor_compra, desconto_percentual,
                    resultado["desconto_valor"], resultado["valor_final"],
                    resultado["cashback_base"], resultado["bonus_vip"],
                    resultado["cashback_pre_dobro"], resultado["dobro_aplicado"],
                    resultado["cashback_final"]
                ))
            conn.commit()

        return jsonify({
            "tipo_cliente": tipo_cliente,
            "valor_compra": valor_compra,
            "desconto_percentual": desconto_percentual,
            **resultado
        })

    except (ValueError, TypeError) as e:
        return jsonify({"erro": f"Dados inválidos: {str(e)}"}), 400
    except Exception:
        logging.exception("Erro ao calcular cashback")
        return jsonify({"erro": "Erro interno do servidor."}), 500


@app.route("/api/historico", methods=["GET"])
def api_historico():
    try:
        ip = request.headers.get("X-Forwarded-For", request.remote_addr)
        if ip and "," in ip:
            ip = ip.split(",")[0].strip()

        with get_db() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT tipo_cliente, valor_compra, desconto_percentual,
                           desconto_valor, valor_final, cashback_base, bonus_vip,
                           cashback_pre_dobro, dobro_aplicado, cashback_final,
                           criado_em
                    FROM consultas
                    WHERE ip = %s
                    ORDER BY criado_em DESC
                    LIMIT 50
                """, (ip,))
                rows = cur.fetchall()

        historico = []
        for r in rows:
            item = dict(r)
            item["valor_compra"] = float(item["valor_compra"])
            item["desconto_percentual"] = float(item["desconto_percentual"])
            item["desconto_valor"] = float(item["desconto_valor"])
            item["valor_final"] = float(item["valor_final"])
            item["cashback_base"] = float(item["cashback_base"])
            item["bonus_vip"] = float(item["bonus_vip"])
            item["cashback_pre_dobro"] = float(item["cashback_pre_dobro"])
            item["cashback_final"] = float(item["cashback_final"])
            
            horario_utc = item["criado_em"].replace(tzinfo=timezone.utc)
            item["criado_em"] = horario_utc.astimezone(FUSO_BRASIL).isoformat()
            historico.append(item)

        return jsonify({"ip": ip, "total": len(historico), "historico": historico})

    except Exception:
        logging.exception("Erro ao buscar histórico")
        return jsonify({"erro": "Erro interno do servidor."}), 500


# Inicialização
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("=" * 50)
    print(f"  Servidor de Cashback iniciado na porta {port}!")
    print("=" * 50)
    app.run(debug=False, host="0.0.0.0", port=port)
