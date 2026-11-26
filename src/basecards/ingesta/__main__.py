"""CLI de ingesta: `python -m basecards.ingesta --csv <archivo> --corte 202609 [--validar]`."""

from __future__ import annotations

import argparse
import json
import os
import sys

from basecards.ingesta.corte import IngestaError, ingerir_csv


def main(argv: list[str] | None = None) -> int:
    for s in (sys.stdout, sys.stderr):  # acentos correctos en la consola de Windows
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(prog="basecards.ingesta", description=__doc__)
    p.add_argument("--csv", default=os.environ.get("CORTE_CSV"), help="CSV del corte (o CORTE_CSV)")
    p.add_argument("--corte", type=int, default=int(os.environ.get("FECHA_CORTE", "202609")))
    p.add_argument("--dir-cortes", default=None, help="Raíz de salida (por defecto data/cortes)")
    p.add_argument(
        "--validar",
        action="store_true",
        help="Corre también la validación determinista E1 del Agente de Soporte",
    )
    args = p.parse_args(argv)
    if not args.csv:
        p.error("falta --csv (o la variable CORTE_CSV)")

    try:
        res = ingerir_csv(args.csv, args.corte, dir_cortes=args.dir_cortes, validar=args.validar)
    except (IngestaError, FileNotFoundError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    m = res.manifiesto
    resumen = {
        "corte": m["corte"],
        "parquet": str(res.ruta_parquet),
        "filas": m["filas"],
        "cohortes": m["cohortes"],
        "mb_csv": round(m["fuente"]["bytes"] / 1e6, 1),
        "mb_parquet": round(m["parquet"]["bytes"] / 1e6, 1),
        "n01_filas_normalizadas": m["n01_filas_normalizadas"],
        "avisos": m["avisos"],
        "validacion": m["validacion"],
        "duracion_s": m["duracion_s"],
    }
    print(json.dumps(resumen, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
