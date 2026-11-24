---
name: seguimiento-cosechas
description: Genera la presentación mensual de seguimiento de cosechas de tarjetas
  (volumen, Over30 y NCL por MOB contra la curva esperada). Úsala cuando el analista
  pida el seguimiento mensual de vintage o de cosechas.
allowed-tools: paquete_validar metricas_calcular graficas_generar pptx_ensamblar
  documento_verificar_cifras documento_convertir documento_miniaturas
salidas: [pptx, pdf]
---
# Seguimiento de cosechas

## Fuentes de verdad
- Columnas, tipos, catálogos y reglas del paquete: `schema.json`.
- Fórmulas, denominadores y formatos: `metrics.yaml`.
- Gráficas, ejes, series, tablas y topes de filas: `charts.yaml`.
- Branding, layouts y estilos: `config/branding/banamex.yaml` y
  `config/branding/template_banamex.pptx`.
- Si `paquete_validar` detecta campos o catálogos nuevos, propón un borrador con
  `skill_proponer_borrador`; no edites la skill aprobada ni copies esos datos aquí.

## Secciones (en este orden)
1. Portada — título y fecha de corte, sin métricas.
2. Resumen ejecutivo — 3 mensajes: la cohorte con mayor desviación, la tendencia de
   Over30 de las cosechas recientes y la comparación champion vs. challenger.
3. Volumen por cosecha — gráfica `volumen_cosecha`.
4. Curvas Over30 vs. esperada — gráfica `curvas_over30`.
5. Curvas NCL vs. esperada — gráfica `curvas_ncl`.
6. Cohortes fuera de intervalo — tabla `fuera_intervalo`.
7. Siguientes pasos — sin cifras nuevas.

## Reglas de redacción
- **Action titles obligatorios:** cada título de una diapositiva de contenido es una oración completa que resume el hallazgo; nunca una etiqueta genérica.
- **Densidad ejecutiva:** máximo 1 gráfica o 1 tabla por diapositiva y máximo 4 viñetas de 40 palabras o menos.
- **Cero números literales:** toda cifra va con marcadores `{{m.<metrica>.<cohorte>.<mob>|<formato>}}`; si escribes un número, el texto se rechaza.
- No compares cosechas con distinto MOB máximo sin decirlo.
