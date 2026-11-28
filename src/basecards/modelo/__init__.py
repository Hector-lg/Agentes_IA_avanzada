"""Núcleo de analítica (Integrante 1): ingesta E1 y MPDC E2 (curva esperada por cohorte y MOB).

- `config`: llaves de cohorte y lectura de config/modelo.yaml y config/indicadores.yaml.
- `indicadores`: indicadores objetivo (tasa NCL, tasa Over30 y margen por cuenta).
- `features`: tabla de entrenamiento y scoring sin fuga de información.
- `metricas`, `baselines`: evaluación y los modelos simples que el MPDC debe superar.
- `entrenar`: datos, 9 modelos cuantílicos + M4, bundle, promoción a champion y CLI.
- `backtest`: validación con origen móvil y calibración CQR (`calibracion`).
- `bundle`: clase `MPDC` para cargar una versión y predecir; `ficha`: ficha del modelo.
- `puntuar`: scoring del corte con el champion, salidas para E3, E5, E6 y dashboards, y CLI.
"""
