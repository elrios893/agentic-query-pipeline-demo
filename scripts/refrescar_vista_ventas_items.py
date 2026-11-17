#!/usr/bin/env python3
"""
scripts/refrescar_vista_ventas_items.py
Refresca la vista materializada ventas_unificada en prueba_analisis.
La vista usa la tabla 'items' (maestra de items) como fuente de GRUPO normalizado.

Uso:
    python scripts/refrescar_vista_ventas_items.py
    python scripts/refrescar_vista_ventas_items.py --dry-run   (solo verificar, sin refrescar)

Retorna exit code 0 si OK, 1 si error.
"""
import os
import sys
import time
import argparse
from pathlib import Path
from datetime import datetime

# Forzar UTF-8 en stdout para evitar errores en consola Windows
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if sys.stderr.encoding and sys.stderr.encoding.lower() != 'utf-8':
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# ---------------------------------------------------------------------------
# Setup de paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv
load_dotenv(BASE_DIR / '.env')

try:
    import psycopg2
except ImportError:
    print('[ERROR] psycopg2 no instalado. Ejecutar: pip install psycopg2-binary')
    sys.exit(1)


# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------
VISTA = 'ventas_unificada'

CONN_PARAMS = dict(
    host=os.getenv('DB_HOST', 'localhost'),
    port=int(os.getenv('DB_PORT', 5432)),
    dbname=os.getenv('DB_NAME', 'prueba_analisis'),
    user=os.getenv('DB_USER', 'postgres'),
    password=os.getenv('DB_PASSWORD', 'postgres'),
)


# ---------------------------------------------------------------------------
# Lógica principal
# ---------------------------------------------------------------------------

def refrescar(dry_run: bool = False) -> bool:
    """
    Ejecuta REFRESH MATERIALIZED VIEW.

    Returns:
        True si éxito, False si error.
    """
    # CONCURRENTLY evita el lock exclusivo que bloquea lecturas mientras
    # refresca -- necesario porque este refresco corre varias veces por
    # manana via tarea programada y puede caer en horario de uso activo.
    # Requiere el indice unico idx_vu_row_hash (ver setup_ventas_unificada.sql).
    sql_refresh = f'REFRESH MATERIALIZED VIEW CONCURRENTLY {VISTA};'
    sql_count   = f'SELECT COUNT(*) FROM {VISTA};'
    sql_cobertura = f"""
        SELECT
            COUNT(*) AS total,
            COUNT(CASE WHEN "TIENE_NORM" THEN 1 END) AS normalizadas,
            ROUND(
                COUNT(CASE WHEN "TIENE_NORM" THEN 1 END)::numeric
                / NULLIF(COUNT(*), 0) * 100, 1
            ) AS pct_cobertura
        FROM {VISTA};
    """

    sql_sin_match = f"""
        SELECT TRIM("REFERENCIA"), MIN(TRIM("DESC_ITEM")), COUNT(*),
               BOOL_OR("GRUPO_NORM" IS NULL)
        FROM {VISTA}
        WHERE NOT "TIENE_NORM"
        GROUP BY 1
        ORDER BY 3 DESC
        LIMIT 50;
    """

    ts_inicio = datetime.now()
    print(f'[{ts_inicio:%Y-%m-%d %H:%M:%S}] Iniciando refresco de {VISTA}...')
    print(f'  Fuente de normalizacion: tabla items (maestra de items)')
    if dry_run:
        print(f'  Modo: DRY-RUN -- solo se verifica la conexion, no se refresca.')

    try:
        conn = psycopg2.connect(**CONN_PARAMS)
        conn.autocommit = True
        cur = conn.cursor()

        # Verificar que la vista existe
        cur.execute(
            "SELECT 1 FROM pg_matviews WHERE matviewname = %s;",
            (VISTA,)
        )
        if not cur.fetchone():
            print(f'[ERROR] La vista materializada "{VISTA}" no existe en {CONN_PARAMS["dbname"]}.')
            print('        Ejecutar primero: scripts/setup_ventas_unificada.sql')
            cur.close()
            conn.close()
            return False

        print(f'  Conexión OK → {CONN_PARAMS["dbname"]} en {CONN_PARAMS["host"]}:{CONN_PARAMS["port"]}')

        if dry_run:
            # Solo contar filas actuales sin refrescar
            cur.execute(sql_count)
            total_actual = cur.fetchone()[0]
            print(f'  Filas actuales en la vista: {total_actual:,}')
            print('  [DRY-RUN] Sin cambios realizados.')
            cur.close()
            conn.close()
            return True

        t0 = time.time()
        print(f'  Ejecutando: {sql_refresh.strip()}')
        try:
            cur.execute(sql_refresh)
        except psycopg2.errors.ObjectNotInPrerequisiteState as e:
            # SQLSTATE 55000: CONCURRENTLY exige un indice unico sin WHERE y la
            # vista no lo tiene (ej. BD creada antes de que setup_ventas_unificada.sql
            # definiera idx_vu_row_hash). Antes el script moria aqui y la vista se
            # quedaba desactualizada sin que nadie lo notara. Se refresca igual,
            # sin CONCURRENTLY (bloquea lecturas mientras corre), y se avisa
            # como crear el indice para volver al refresco sin bloqueo.
            print(f'  [AVISO] REFRESH CONCURRENTLY no disponible: {str(e).splitlines()[0]}')
            print('          Se refresca SIN CONCURRENTLY (bloquea lecturas durante el refresco).')
            print('          Para evitarlo, crear el indice una vez:')
            print(f'            CREATE UNIQUE INDEX IF NOT EXISTS idx_vu_row_hash ON {VISTA} (row_hash);')
            t0 = time.time()
            cur.execute(f'REFRESH MATERIALIZED VIEW {VISTA};')
        duracion = round(time.time() - t0, 1)

        # Estadísticas post-refresco
        cur.execute(sql_cobertura)
        row = cur.fetchone()
        total        = int(row[0]) if row else 0
        normalizadas = int(row[1]) if row else 0
        pct          = float(row[2]) if row and row[2] else 0.0

        ts_fin = datetime.now()
        print(f'[{ts_fin:%Y-%m-%d %H:%M:%S}] Refresco completado en {duracion}s.')
        print(f'  Filas totales:       {total:,}')
        print(f'  Filas normalizadas:  {normalizadas:,} ({pct}%)')
        print(f'  Sin normalizar:      {total - normalizadas:,} (usan GRUPO original)')

        # Referencias de ventas que la maestra 'items' no conoce: es la lista que
        # hay que entregar a quien mantiene la maestra para que las agregue.
        # Mientras no esten, esas filas usan el GRUPO/LINEA crudo de ventas (y si
        # GRUPO viene nulo, no aparecen en los cortes por categoria).
        if total - normalizadas > 0:
            cur.execute(sql_sin_match)
            faltantes = cur.fetchall()
            print(f'  Referencias que faltan en la maestra items ({len(faltantes)}):')
            for ref, desc, filas, grupo_nulo in faltantes:
                nota = ' [GRUPO nulo]' if grupo_nulo else ''
                print(f'    {ref}  {desc or ""}  ({filas:,} filas){nota}')

        cur.close()
        conn.close()
        return True

    except psycopg2.OperationalError as e:
        print(f'[ERROR] No se pudo conectar a la base de datos: {e}')
        return False
    except psycopg2.Error as e:
        print(f'[ERROR] Error de PostgreSQL: {e}')
        return False
    except Exception as e:
        print(f'[ERROR] Error inesperado: {e}')
        return False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Refresca la vista materializada ventas_unificada.'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Solo verificar conexion y mostrar estado actual sin refrescar.',
    )
    args = parser.parse_args()

    ok = refrescar(dry_run=args.dry_run)
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
