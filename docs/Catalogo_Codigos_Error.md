# Catálogo de códigos de error

Este documento explica, en lenguaje llano, **qué pasó**, **dónde ocurrió** y **por qué** cuando una herramienta de la plataforma devuelve un código de error.

Está dirigido a tres públicos:

- **Desarrolladores:** para saber qué módulo lanzó el error y qué condición lo provocó.
- **Soporte y operación:** para diagnosticar un fallo con el código que aparece en el log o en la pantalla.
- **Analistas:** para entender por qué un archivo no se generó y qué deben corregir.

> **Fuente de verdad:** las constantes y los mensajes viven en el código de `src/basecards/herramientas/`. Este catálogo los documenta; si un código cambia en el código, este documento debe actualizarse en el mismo cambio.

---

## 1. Qué es un código de error

Cada herramienta lanza una excepción con un código estable, por ejemplo:

```text
E-CON-003: soffice terminó con código 1: Error: source file could not be loaded
```

El código no cambia entre versiones. Sirve para:

- buscar el caso exacto en este catálogo;
- filtrar logs y métricas;
- escribir pruebas automatizadas;
- distinguir un error del usuario (ruta mala, archivo corrupto) de un error de infraestructura (falta LibreOffice, sandbox caído).

El texto que acompaña al código es para humanos y puede incluir la ruta, el campo o el valor que falló. **No contiene secretos** ni rutas fuera del workspace del usuario.

---

## 2. Cómo leer un código

### 2.1 Formato

```text
E-<FAMILIA>-<NNN>
```

| Parte | Significado |
|---|---|
| `E` | Es un error. Los códigos que empiezan con `W` son advertencias y no bloquean el flujo. |
| `<FAMILIA>` | Módulo o tipo de herramienta que lo lanzó. |
| `<NNN>` | Número de caso dentro de la familia. |

### 2.2 Familias

| Prefijo | Módulo | Herramientas principales |
|---|---|---|
| `E-CON` | `src/basecards/herramientas/conversion.py` | `documento_convertir`, `documento_miniaturas` |
| `E-VER` | `src/basecards/herramientas/verificacion.py` | `documento_verificar_cifras` |
| `E-PPT` | `src/basecards/herramientas/pptx_ensamblar.py` | `pptx_ensamblar` |
| `E-DOC` | `src/basecards/herramientas/docx_ensamblar.py` | `docx_ensamblar` |
| `E-XLS` | `src/basecards/herramientas/xlsx_ensamblar.py` | `xlsx_ensamblar` |
| `E-SKL` | `src/basecards/herramientas/skills.py` | `skill_validar`, `skills_leer`, `skills_listar` |
| `E-MET` | `src/basecards/herramientas/metricas.py` | `metricas_calcular` |
| `E-MAR` | `src/basecards/herramientas/marcadores.py` | Sustitución de marcadores; se traduce a `E-PPT-030`, `E-DOC-030` o `E-XLS-030` |

### 2.3 Anatomía de la excepción

Todas las excepciones de herramientas exponen los mismos atributos:

```python
class PptxError(Exception):
    def __init__(self, codigo: str, mensaje: str, ruta: str = "") -> None:
        super().__init__(f"{codigo}: {mensaje}")
        self.codigo = codigo      # "E-PPT-002"
        self.mensaje = mensaje    # "falta el branding ..."
        self.ruta = ruta          # "branding", "plantilla", "SKILL.md", ...
```

| Atributo | Uso |
|---|---|
| `codigo` | Identificador estable para logs, filtros y pruebas. |
| `mensaje` | Explicación legible de qué falló. |
| `ruta` | Archivo, campo o sección donde ocurrió. Puede venir vacío. |
| `str(exc)` | Concatenación `codigo: mensaje`, lista para el log. |

`documento_verificar_cifras` es la excepción a la regla: cuando encuentra cifras que no cuadran **no lanza una excepción**; devuelve un `ResultadoVerificacion` con `discrepancias > 0`. Solo los fallos estructurales (archivo inexistente, corrupto, formato no soportado) lanzan `E-VER-*`.

### 2.4 Dónde se ve el código

- **Log del worker:** una línea por ejecución con `run_id`, `user_id`, herramienta, `codigo`, `mensaje` y `ruta`.
- **Respuesta de la API:** el cuerpo del error conserva `codigo`, `mensaje` y `ruta`.
- **Interfaz:** la pantalla muestra el mensaje y, debajo, el código para que soporte lo pueda citar.
- **Pruebas:** los tests comparan `excinfo.value.codigo` contra la constante, no contra el texto.

---

## 3. Diagnóstico rápido

| Síntoma | Códigos probables | Sección |
|---|---|---|
| “No existe el archivo” | `E-CON-001`, `E-VER-001`, `E-SKL-001`, `E-MET-001` | 4.1, 4.2, 4.6, 4.7 |
| “El archivo no abre” o “está corrupto” | `E-CON-002`, `E-VER-002`, `E-PPT-002`, `E-DOC-002`, `E-XLS-051`, `E-DOC-051` | 4.1–4.5 |
| El sandbox no ejecuta o no está disponible | `E-CON-005`, `E-CON-006`, `E-CON-007`, `E-CON-012` | 4.1 |
| `soffice` o `pdftoppm` fallaron | `E-CON-003`, `E-CON-004`, `E-CON-011`, `E-CON-013`, `E-CON-014` | 4.1 |
| Un marcador quedó sin valor o con formato inválido | `E-MAR-030` → `E-PPT-030`, `E-DOC-030`, `E-XLS-030` | 4.8 |
| La métrica o su fórmula están mal escritas | `E-MET-010` … `E-MET-014` | 4.7 |
| La skill no valida | `E-SKL-001` … `E-SKL-062` | 4.6 |
| Las cifras del documento no cuadran | `E-VER-001` … `E-VER-006` | 4.2 |
| El `.pptx` no cumple el contrato de secciones | `E-PPT-010`, `E-PPT-060` | 4.3 |
| El `.xlsx` no cumple la estructura esperada | `E-XLS-060` | 4.5 |

---

## 4. Catálogo por familia

### 4.1 `E-CON` — Conversión a PDF y miniaturas

**Módulo:** `src/basecards/herramientas/conversion.py`
**Excepción:** `ConversionError`
**Herramientas:** `documento_convertir`, `documento_miniaturas`

Estas herramientas convierten un `.pptx` ya verificado y aprobado a `.pdf` y generan una imagen PNG por diapositiva. Los comandos se ejecutan en el sandbox sin red; nunca en el proceso del agente.

| Código | Dónde ocurre | Qué pasó | Por qué | Qué hacer |
|---|---|---|---|---|
| `E-CON-001` | `_validar_pptx`, antes del sandbox | El archivo no existe o la ruta es un directorio | Ruta mal escrita, archivo borrado o se pasó una carpeta | Verificar la ruta y que el `.pptx` exista; volver a subirlo |
| `E-CON-002` | `_validar_pptx`, antes del sandbox | El archivo no es un `.pptx` válido: extensión distinta, no se puede leer, no tiene firma ZIP, está corrupto o le faltan `[Content_Types].xml` o `ppt/presentation.xml` | Archivo renombrado, descarga incompleta, ZIP dañado o formato distinto | Abrirlo en PowerPoint o LibreOffice y volver a exportarlo como `.pptx` |
| `E-CON-003` | `_ejecutar`, comando `soffice` | `soffice` terminó con código distinto de 0 | Deck dañado, fuente no disponible, error interno de LibreOffice | Revisar el `stderr` del mensaje; abrir el archivo y simplificar la diapositiva que falla |
| `E-CON-004` | Después de `soffice` | El PDF no se generó o quedó vacío | LibreOffice terminó “bien” pero no escribió el archivo, el directorio de salida no era el esperado o faltan permisos | Revisar el workspace y los logs del sandbox; reintentar la conversión |
| `E-CON-005` | `_resolver_runner` o `_ejecutar` | No se inyectó un `SandboxRunner`, o el runner lanzó una excepción no controlada | Worker mal configurado, `sandbox-runner` caído o error de comunicación | Inyectar el cliente del sandbox; revisar la salud de `sandbox-runner`. Nunca se ejecuta LibreOffice en el host como respaldo |
| `E-CON-006` | `_ejecutar` | El comando excedió `timeout_s` | Deck muy grande, LibreOffice se colgó o el timeout es demasiado bajo | Reintentar con un timeout mayor (máximo 120 s según el Manual Técnico) o dividir el deck |
| `E-CON-007` | `_ejecutar`, comando `soffice` | `soffice` no está disponible: `FileNotFoundError` o código 127 | La imagen `bc/sandbox` no incluye LibreOffice o el binario no está en `PATH` | Reconstruir la imagen con `libreoffice-impress-nogui` y verificar `soffice --version` dentro del contenedor |
| `E-CON-008` | `documento_convertir` | La salida no termina en `.pdf`, no se pudo crear el directorio de salida o no se pudo mover el PDF al nombre final | Extensión equivocada, ruta sin permisos o archivo bloqueado | Usar una ruta `.pdf` dentro del workspace y verificar permisos |
| `E-CON-010` | `documento_miniaturas` | El directorio de salida es un archivo, no se pudo crear el directorio o el temporal, no se pudieron ajustar permisos o no se pudo limpiar una miniatura previa | Colisión de nombres, permisos insuficientes o archivo bloqueado | Pasar un directorio escribible; revisar que no exista un archivo con el mismo nombre |
| `E-CON-011` | `_ejecutar`, comando `pdftoppm` | `pdftoppm` terminó con código distinto de 0 | PDF dañado o error de Poppler | Verificar el PDF intermedio y reintentar la conversión |
| `E-CON-012` | `_ejecutar`, comando `pdftoppm` | `pdftoppm` no está disponible: `FileNotFoundError` o código 127 | Falta `poppler-utils` en la imagen del sandbox | Reconstruir la imagen con `poppler-utils` |
| `E-CON-013` | `_renombrar_miniaturas` | `pdftoppm` no generó ningún PNG reconocible | El PDF no tiene páginas, el prefijo de salida no coincide o el comando no escribió en el directorio esperado | Revisar el PDF y el `stderr`; confirmar que `pdftoppm` puede escribir en el workspace |
| `E-CON-014` | `_renombrar_miniaturas` | No se pudo renombrar una miniatura al nombre estable `diapositiva-NN.png` | Permisos, archivo bloqueado o colisión de nombres | Revisar permisos del workspace y reintentar |

> El código `E-CON-009` no existe; el hueco queda reservado para no renumerar los códigos ya publicados.

---

### 4.2 `E-VER` — Verificación de cifras

**Módulo:** `src/basecards/herramientas/verificacion.py`
**Excepción:** `VerificacionError`
**Herramienta:** `documento_verificar_cifras`

Esta herramienta implementa RB-06: compara cada número del archivo final contra los valores calculados. Si encuentra diferencias, **devuelve** un `ResultadoVerificacion` con `discrepancias > 0`; no lanza excepción. Los códigos de esta familia cubren solo fallos estructurales.

| Código | Dónde ocurre | Qué pasó | Por qué | Qué hacer |
|---|---|---|---|---|
| `E-VER-001` | `documento_verificar_cifras` | El archivo no existe o no es un archivo | Ruta mal escrita o archivo borrado | Verificar la ruta; volver a generar el documento |
| `E-VER-002` | Extractores de `.pptx`, `.docx`, `.xlsx` y `.pdf` | El archivo no se pudo abrir con su librería | Archivo corrupto, contraseña, formato distinto o versión incompatible | Abrirlo con la aplicación de origen y volver a exportarlo |
| `E-VER-003` | `documento_verificar_cifras` | No se proporcionaron valores esperados | El diccionario `valores_esperados` llegó vacío | Calcular las métricas antes de verificar y pasar el diccionario completo |
| `E-VER-004` | `documento_verificar_cifras` | La extensión no está soportada | Se pasó un formato fuera de `.pptx`, `.docx`, `.xlsx`, `.pdf`, `.md`, `.html` | Convertir al formato soportado o ampliar la herramienta |
| `E-VER-005` | Reservado | Reservado para “gráfica sin datos numéricos” | Definido, aún no se lanza | No se usa todavía; no reutilizar el número |
| `E-VER-006` | Reservado | Reservado para “error de lectura general” | Definido, aún no se lanza | No se usa todavía; no reutilizar el número |

**Cómo se lee un resultado con discrepancias:** el campo `detalle` trae una lista de `Discrepancia` con `ubicacion`, `origen`, `esperado`, `encontrado` y `marcador`. Por ejemplo:

```text
diapositiva 3, tabla, fila 2, columna 1: esperado 0.0234, encontrado 0.0241 ({{m.tasa_ncl.202401.MOB12|pct2}})
```

Eso no es un `E-VER-*`: es un resultado de negocio que obliga a corregir la narrativa o el cálculo antes de publicar.

---

### 4.3 `E-PPT` — Presentaciones `.pptx`

**Módulo:** `src/basecards/herramientas/pptx_ensamblar.py`
**Excepción:** `PptxError`
**Herramienta:** `pptx_ensamblar`

| Código | Dónde ocurre | Qué pasó | Por qué | Qué hacer |
|---|---|---|---|---|
| `E-PPT-001` | `pptx_ensamblar` | No existe la carpeta de la skill | Nombre mal escrito o skill no aprobada | Verificar el nombre y que la skill esté en `skills/aprobadas/` |
| `E-PPT-002` | Carga de branding y plantilla | Falta el branding o la plantilla, no se pueden leer, el branding no es un mapa YAML, un color es inválido o la plantilla no abre | Configuración incompleta, YAML mal formado o archivo `.pptx` dañado | Revisar `config/branding/banamex.yaml`, los colores `#RRGGBB` y la plantilla base |
| `E-PPT-003` | Apertura de plantilla y placeholders | La plantilla no tiene los layouts requeridos (`Portada`, `Titulo_y_Grafica`, `Dos_Columnas`, `Tabla`, `Cierre`) o falta un placeholder | Plantilla editada o versión equivocada | Usar la plantilla institucional con los cinco layouts y sus placeholders |
| `E-PPT-010` | `_leer_secciones_skill` | Falta `SKILL.md`, no se puede leer, no declara las 7 secciones del contrato o una sección declara otro recurso | Skill incompleta o desalineada con el contrato de presentación | Corregir `SKILL.md`: 7 secciones en orden y el recurso indicado en cada una |
| `E-PPT-011` | `_cargar_charts` | Falta `charts.yaml`, no se puede leer o no cumple el esquema | YAML mal formado o gráfica/tabla mal declarada | Corregir `charts.yaml` según el esquema de `GraficasDoc` |
| `E-PPT-020` | Validación de contenido | La estructura del contenido es inválida: una sección no es un mapa, tiene claves no soportadas, las viñetas no son lista de textos, faltan las dos columnas del resumen, una nota no es texto o `contenido`/`graficas` no son mapas | El JSON narrativo no respeta el contrato de la sección | Revisar las claves permitidas de cada sección y el tipo de cada valor |
| `E-PPT-021` | `_titulo_texto`, portada | Una sección o la portada tiene título vacío | El LLM o el insumo no proporcionó título | Escribir un título no vacío |
| `E-PPT-030` | Marcadores | Un marcador tiene formato desconocido, el valor no es numérico, el marcador no tiene valor calculado, quedaron marcadores sin sustituir o `metricas` no es un mapa | Métrica no calculada, formato inválido o diccionario `m` incompleto | Calcular todas las métricas y usar solo formatos `pct1`, `pct2`, `mxn`, `mxn_millones` o `int` |
| `E-PPT-031` | `_validar_sin_marcadores` | Un campo que debe ser narrativo contiene `{{m.` | Se intentó poner una métrica donde solo va texto (título o subtítulo) | Quitar el marcador; las métricas van en los campos que las admiten |
| `E-PPT-032` | `_leer_vinetas` | Una sección supera el máximo de viñetas | El texto es demasiado largo para la diapositiva | Reducir el número de viñetas según `branding.reglas.max_vinetas` |
| `E-PPT-033` | `_leer_vinetas` | Una viñeta supera el máximo de palabras | Frase demasiado larga | Acortar la viñeta según `branding.reglas.max_palabras_vineta` |
| `E-PPT-040` | `_crear_grafica`, `_crear_tabla` | El contenido trae datos de una gráfica o tabla que no está declarada en `charts.yaml` | Nombre mal escrito o `charts.yaml` incompleto | Declarar la gráfica o tabla en `charts.yaml` o corregir el nombre |
| `E-PPT-041` | `_crear_grafica`, `_crear_tabla` | Falta la gráfica o tabla que la sección declara | El contenido no incluyó los datos correspondientes | Generar los datos de la gráfica o tabla antes de ensamblar |
| `E-PPT-042` | `_insertar_grafica` | La gráfica es inválida o no se pudo crear: falta la imagen, la serie no es lista de números, faltan categorías o series, el tipo no se puede crear de forma nativa, los valores y categorías no coinciden o python-pptx falló | `charts.yaml` o los datos de la gráfica no respetan el contrato | Revisar tipo, categorías, series y longitudes; para tipos no nativos, generar una imagen con matplotlib |
| `E-PPT-043` | `_insertar_tabla` | La tabla es inválida o no se pudo crear: faltan columnas o filas, los formatos son inválidos, una fila no tiene el número de valores esperado o python-pptx falló | Datos de tabla incompletos o mal formados | Revisar columnas, filas y formatos de la tabla |
| `E-PPT-050` | `pptx_ensamblar` | La salida no termina en `.pptx` o no se pudo guardar | Extensión equivocada, ruta sin permisos o archivo bloqueado | Usar una ruta `.pptx` escribible |
| `E-PPT-060` | `pptx_ensamblar` | Se generaron menos o más diapositivas de las 7 del contrato | Alguna sección no se creó o se creó dos veces | Revisar el orden y la creación de las 7 secciones |

---

### 4.4 `E-DOC` — Documentos `.docx`

**Módulo:** `src/basecards/herramientas/docx_ensamblar.py`
**Excepción:** `DocxError`
**Herramienta:** `docx_ensamblar`

| Código | Dónde ocurre | Qué pasó | Por qué | Qué hacer |
|---|---|---|---|---|
| `E-DOC-002` | Carga de branding y plantilla | Falta el branding o la plantilla, no se pueden leer, el branding no es un mapa, no declara la sección `documento`, un color o tamaño es inválido o la plantilla no abre | Configuración incompleta o archivo dañado | Revisar `config/branding/banamex.yaml` y la plantilla `.docx` |
| `E-DOC-003` | Apertura de plantilla | La plantilla no tiene los estilos requeridos (`Normal`, `Title`, `Subtitle`, `Heading 1`, `List Bullet`, `Table Grid`) | Plantilla editada o versión equivocada | Usar la plantilla institucional con esos estilos |
| `E-DOC-010` | `docx_ensamblar` | `analisis` no es un mapa con el JSON del Copiloto | El insumo llegó como texto, lista o vacío | Pasar el JSON del Copiloto como diccionario |
| `E-DOC-020` | Validación del JSON | Un campo es inválido: texto vacío, tipo equivocado, número fuera de rango, `metadatos` no es mapa, `rentabilidad_esperada` no es número/texto o `requiere_revision_humana` no es booleano | El JSON no respeta el contrato del memo | Corregir el campo indicado en el mensaje |
| `E-DOC-030` | Marcadores | Un marcador tiene formato desconocido, el valor no es numérico, no tiene valor calculado, quedaron marcadores sin sustituir o `metricas` no es un mapa | Métrica no calculada o diccionario `m` incompleto | Calcular todas las métricas y usar los formatos permitidos |
| `E-DOC-040` | `analisis.evidencia`, `analisis.drivers` | La evidencia o los drivers tienen estructura inválida: no son listas, un elemento no es mapa/texto, falta `hallazgo` o `feature`, o `contribucion` no es numérico/texto | El JSON del Copiloto está incompleto | Corregir la lista y cada elemento según el contrato |
| `E-DOC-050` | `docx_ensamblar` | La salida no termina en `.docx` o no se pudo guardar | Extensión equivocada, ruta sin permisos o archivo bloqueado | Usar una ruta `.docx` escribible |
| `E-DOC-051` | Verificación posterior al guardado | El archivo generado no se puede volver a abrir con `python-docx` | El `.docx` quedó corrupto o incompleto | Revisar el guardado y reintentar; si persiste, reportar con el archivo |

---

### 4.5 `E-XLS` — Libros `.xlsx`

**Módulo:** `src/basecards/herramientas/xlsx_ensamblar.py`
**Excepción:** `XlsxError`
**Herramienta:** `xlsx_ensamblar`

| Código | Dónde ocurre | Qué pasó | Por qué | Qué hacer |
|---|---|---|---|---|
| `E-XLS-002` | Carga de branding | Falta el branding, no se puede leer, no es un mapa YAML o un color es inválido | Configuración incompleta o YAML mal formado | Revisar `config/branding/banamex.yaml` |
| `E-XLS-010` | Validación de `datos` | `datos` está vacío, no es lista/mapa, las columnas son inválidas o repetidas, las filas no son listas, hay columnas no declaradas, el número de valores no coincide o hay un tipo no soportado | Los datos de entrada no respetan el contrato | Corregir la estructura de `datos` y los tipos de cada celda |
| `E-XLS-020` | Validación de formatos y resumen | Un campo obligatorio no es texto, el formato es desconocido, `formatos` no es mapa, una columna de formato no existe en `datos`, `resumen` es inválido o su numerador/denominador no es columna de `Datos` | Formato o resumen mal configurado | Usar un alias de formato válido o un formato de Excel; revisar las columnas del resumen |
| `E-XLS-030` | Marcadores | Un marcador tiene formato desconocido, quedó sin sustituir o `metricas` no es un mapa | Métrica no calculada o diccionario `m` incompleto | Calcular todas las métricas y revisar los formatos |
| `E-XLS-040` | Resumen | Una columna del resumen no es numérica o una razón tiene denominador cero | Los datos no permiten calcular la razón | Revisar la columna o excluir la razón cuando el denominador sea cero |
| `E-XLS-050` | `xlsx_ensamblar` | La salida no termina en `.xlsx`, no se pudo crear o no se pudo generar | Extensión equivocada, ruta sin permisos o error de `xlsxwriter` | Usar una ruta `.xlsx` escribible y revisar el detalle |
| `E-XLS-051` | Verificación posterior al guardado | El archivo generado no se puede volver a abrir con `openpyxl` | El `.xlsx` quedó corrupto o incompleto | Revisar el guardado y reintentar; si persiste, reportar con el archivo |
| `E-XLS-060` | Verificación estructural | El libro no tiene las hojas esperadas, no tiene encabezados congelados, el autofiltro no coincide, falta un encabezado, un formato no es el esperado, una fórmula no coincide, no tiene valor cacheado o el valor cacheado no coincide con el cálculo de Python | El libro generado no cumple el contrato de calidad | Corregir la generación; no publicar el archivo hasta que la verificación pase |

---

### 4.6 `E-SKL` — Skills

**Módulo:** `src/basecards/herramientas/skills.py`
**Excepción:** `SkillError`
**Herramientas:** `skill_validar`, `skills_leer`, `skills_listar`

`skill_validar` **acumula** los hallazgos y devuelve un `ResultadoValidacion` con `errores` y `advertencias`; no se detiene en el primer problema. `skills_leer` y `skills_listar` sí lanzan `SkillError` cuando no pueden ubicar o leer la skill.

| Código | Dónde ocurre | Qué pasó | Por qué | Qué hacer |
|---|---|---|---|---|
| `E-SKL-001` | `skill_validar`, `_leer_texto`, `_ubicar_skill` | Falta un archivo obligatorio o no se encontró la skill en aprobadas ni en borradores del usuario | Skill incompleta, nombre mal escrito o ruta equivocada | Crear los cinco archivos obligatorios o corregir el nombre |
| `E-SKL-002` | `_validar_nombre`, `_validar_frente`, `skill_validar` | El nombre de la skill es inválido o no coincide con la carpeta | Nombre con mayúsculas, espacios o caracteres no permitidos; `name` distinto al directorio | Usar minúsculas, números y guiones (2–64 caracteres) y hacer que coincida con la carpeta |
| `E-SKL-003` | `_partir_skill_md`, `_validar_frente` | El encabezado YAML de `SKILL.md` es inválido, falta `description` o supera 1024 caracteres | Frontmatter mal formado o descripción ausente/demasiado larga | Corregir el bloque `---` y la descripción |
| `E-SKL-004` | `_validar_frente` | Falta `allowed-tools` o incluye una herramienta desconocida | El encabezado no declara las herramientas permitidas o usa un nombre inválido | Declarar solo herramientas de `HERRAMIENTAS_VALIDAS` |
| `E-SKL-005` | `_validar_frente` | Falta `salidas` o incluye una salida no permitida | El encabezado no declara los formatos de salida o usa uno inválido | Declarar solo salidas de `SALIDAS_VALIDAS` |
| `E-SKL-010` | `_validar_esquema_paquete`, `skill_validar` | `schema.json` no es un objeto JSON o no cumple el metaesquema | JSON mal formado o estructura inválida | Corregir el JSON según el metaesquema del paquete |
| `E-SKL-011` | `_validar_esquema_paquete` | `schema.json` es inconsistente: `min` mayor que `max`, una llave no existe en columnas o una llave no es dimensión | Definición de columnas o llave contradictoria | Corregir los rangos y la llave |
| `E-SKL-012` | `_validar_esquema_paquete` | Una columna referencia un catálogo que no está definido | Falta el catálogo en `catalogos` | Definir el catálogo o corregir la referencia |
| `E-SKL-013` | `_validar_esquema_paquete` | Una regla usa una columna que no existe | Expresión o condición con un identificador inválido | Corregir la regla o declarar la columna |
| `E-SKL-020` | `_cargar_yaml` | `metrics.yaml` no es YAML válido, no es un mapa o no cumple el modelo `MetricasDoc` | YAML mal formado o campos faltantes | Corregir `metrics.yaml` según el DSL |
| `E-SKL-021` | `_validar_metricas`, `_validar_graficas` | Una métrica, agrupación, indicador o serie referencia una columna o métrica inexistente | Nombre mal escrito o schema incompleto | Corregir la referencia o declarar la columna/métrica |
| `E-SKL-022` | `_validar_expresion` | La expresión de una métrica es inválida: carácter no permitido, expresión vacía, paréntesis desbalanceados, función sin paréntesis o función no permitida | Fórmula mal escrita o uso de una función fuera de `sum` y `count` | Corregir la expresión; solo se admiten columnas, números, `+ - * /`, paréntesis, `sum` y `count` |
| `E-SKL-023` | `_validar_metricas` | Una métrica usa un formato desconocido | Formato fuera de `pct1`, `pct2`, `mxn`, `mxn_millones`, `int` | Usar uno de los formatos permitidos |
| `E-SKL-030` | `_cargar_yaml`, `_validar_graficas` | `charts.yaml` no es YAML válido, no cumple el modelo `GraficasDoc`, una gráfica no declara series o una tabla no declara columnas | YAML mal formado o gráfica/tabla incompleta | Corregir `charts.yaml` |
| `E-SKL-031` | `_validar_graficas` | Una serie usa una métrica que no existe en `metrics.yaml` | Nombre mal escrito o métrica no declarada | Corregir el nombre o declarar la métrica |
| `E-SKL-032` | `_validar_graficas` | La configuración de la gráfica es inconsistente: un tipo nativo no está marcado como nativo, un tipo de imagen no usa `matplotlib` o `eje_x` no es dimensión | `charts.yaml` contradice el tipo de gráfica o el schema | Corregir `nativa`, `motor` o `eje_x` |
| `E-SKL-040` | `_validar_marcadores` | Un marcador de `SKILL.md` tiene formato inválido, métrica inexistente, cohorte inválida, MOB inválido o fuera de rango, o formato desconocido | El texto de la skill usa marcadores que no se podrán sustituir | Corregir el marcador: `{{m.metrica.AAAAMM.MOBn|formato}}` |
| `E-SKL-050` | `_cargar_metadata` | `metadata.json` no es JSON válido o no cumple el modelo `MetadataSkill` | JSON mal formado o campos faltantes | Corregir `metadata.json` |
| `E-SKL-051` | `_validar_integridad` | El hash de contenido no coincide, el hash de la plantilla no coincide o se declaró `hash_template` sin `template.pptx` | Los archivos cambiaron después de firmar la skill | Recalcular `hash_contenido` y `hash_template` con las funciones del módulo |
| `E-SKL-052` | `_validar_integridad` | `metadata.diccionario` no coincide con `schema.json` | Metadatos desactualizados | Actualizar `metadata.json` |
| `E-SKL-053` | `skills_leer` | La versión solicitada es inválida o no está disponible en disco | Versión con formato incorrecto o distinta a la publicada | Usar una versión `X.Y.Z` que exista |
| `E-SKL-060` | `_ubicar_skill`, `skill_validar` | La ruta queda fuera del catálogo o la skill no existe | Intento de salir de `skills/` o ruta equivocada | Usar una ruta dentro del catálogo |
| `E-SKL-061` | `_leer_texto` | Un archivo de texto supera el tamaño máximo o no es UTF-8 válido | Archivo demasiado grande o con otra codificación | Reducir el archivo o guardarlo en UTF-8 |
| `E-SKL-062` | `_validar_usuario`, `skills_listar` | El `user_id` es inválido o falta cuando se piden borradores | Identidad mal formada o ausente | Usar un `user_id` válido del `RunContext` |

---

### 4.7 `E-MET` — Métricas y DSL

**Módulo:** `src/basecards/herramientas/metricas.py`
**Excepción:** `MetricaError`
**Herramienta:** `metricas_calcular`

| Código | Dónde ocurre | Qué pasó | Por qué | Qué hacer |
|---|---|---|---|---|
| `E-MET-001` | Carga de skill, `metrics.yaml` o `schema.json` | Falta un archivo o no existe la skill | Ruta equivocada o skill incompleta | Verificar la ruta y los archivos de la skill |
| `E-MET-002` | Carga y validación | `metrics.yaml` o `schema.json` son inválidos: YAML/JSON mal formado, no son mapas, no declaran columnas, falta `expresion` o `numerador`/`denominador`, no declaran `MOB`/`VINTAGE` o el mínimo de exposición no es finito | Contrato de la skill roto | Corregir el archivo indicado en el mensaje |
| `E-MET-003` | `metricas_calcular` | La cohorte es inválida | No es `AAAAMM` ni `todas` | Usar una cohorte de seis dígitos o `todas` |
| `E-MET-004` | `metricas_calcular` | El rango MOB es inválido: no son enteros, `mob_min > mob_max` o `mob_max` supera el máximo del schema | Parámetros fuera de rango | Corregir `mob_min` y `mob_max` |
| `E-MET-010` | Tokenizador del DSL | La expresión contiene un carácter no permitido | Fórmula con símbolos fuera del DSL | Usar solo columnas, números, `+ - * /`, paréntesis, `sum` y `count` |
| `E-MET-011` | Parser del DSL | La expresión tiene un error de sintaxis: está vacía, hay un token inesperado, falta un token, se esperaba un número/función/paréntesis o falta el nombre de columna | Fórmula incompleta o mal escrita | Corregir la expresión según el DSL |
| `E-MET-012` | Parser y validación | Una columna no existe en el schema o es inválida | Nombre mal escrito o columna no declarada | Corregir el nombre o declarar la columna |
| `E-MET-013` | Parser | Se usó una función no permitida | Solo se admite `sum` y `count` | Cambiar la función o calcularlo en otra métrica |
| `E-MET-014` | Validación de métricas | El formato de una métrica es desconocido | Formato fuera de los permitidos | Usar `pct1`, `pct2`, `mxn`, `mxn_millones` o `int` |
| `E-MET-020` | Carga de datos | La fuente es inválida: no existe, la extensión no está soportada, el nombre de tabla es inválido, DuckDB no tiene tablas, tiene varias tablas sin `tabla` o no se pudo abrir | Ruta, extensión o tabla equivocada | Usar `.parquet`, `.csv`, `.duckdb` o `.db`; indicar `tabla` si hay varias |
| `E-MET-021` | Ejecución de DuckDB | La consulta falló | Columna/tipo incompatible, dato corrupto o error de DuckDB | Revisar el mensaje, el schema y la fuente de datos |

---

### 4.8 `E-MAR` — Marcadores

**Módulo:** `src/basecards/herramientas/marcadores.py`
**Excepción:** `MarcadorError`
**Código:** `E-MAR-030`

`MarcadorError` es interno: `pptx_ensamblar`, `docx_ensamblar` y `xlsx_ensamblar` lo capturan y lo vuelven a lanzar como `E-PPT-030`, `E-DOC-030` o `E-XLS-030`, respectivamente. El código `E-MAR-030` aparece cuando se usa el módulo de marcadores directamente.

| Código | Dónde ocurre | Qué pasó | Por qué | Qué hacer |
|---|---|---|---|---|
| `E-MAR-030` | `formatear_valor`, `sustituir_marcadores` | El formato es desconocido, el valor no es numérico o el marcador no tiene valor calculado | Métrica no calculada o formato inválido | Calcular la métrica y usar un formato permitido |

---

## 5. Advertencias `W-SKL`

Las advertencias no bloquean la validación, pero indican que la skill puede mejorar o que una comprobación no se pudo hacer.

| Código | Qué significa | Qué hacer |
|---|---|---|
| `W-SKL-100` | No hay `template.pptx`; se usará la plantilla institucional de respaldo | Agregar `template.pptx` si la skill necesita una plantilla propia |
| `W-SKL-101` | Falta la carpeta `ejemplos/` o `tests/` | Agregarlas si la skill debe incluir ejemplos o pruebas |
| `W-SKL-102` | No se pudo cruzar `metrics.yaml` o `charts.yaml` porque `schema.json` es inválido | Corregir primero `schema.json` |
| `W-SKL-103` | Una gráfica de `charts.yaml` no se menciona en `SKILL.md` | Mencionarla en el cuerpo de la skill o eliminarla |
| `W-SKL-104` | `metadata.salidas` no coincide con el encabezado de `SKILL.md` | Sincronizar ambos |
| `W-SKL-105` | Hay `template.pptx` pero `hash_template` es `null` | Calcular y declarar el hash |

---

## 6. Cómo se propagan y cómo se registran

1. La herramienta detecta la condición y lanza `XxxError(codigo, mensaje, ruta)`.
2. El nodo del grafo o el worker captura la excepción.
3. El worker registra una línea estructurada:

   ```text
   nivel=error run_id=... user_id=... herramienta=documento_convertir
   codigo=E-CON-003 ruta=/workspace/presentacion.pptx
   mensaje="soffice terminó con código 1: ..."
   ```

4. La API devuelve el código y el mensaje al cliente.
5. La interfaz muestra el mensaje y el código; el detalle técnico queda en el log.
6. Si el error es de negocio (cifras que no cuadran), no hay excepción: el resultado trae `discrepancias` y el flujo vuelve al paso de redacción.

**Regla de oro:** el `codigo` es para la máquina y para soporte; el `mensaje` es para la persona. No se debe parsear el texto del mensaje para decidir qué hacer: se compara el `codigo`.

---

## 7. Reglas para agregar o cambiar códigos

- **Un código publicado no se reutiliza ni se renumera.** Si un caso desaparece, el código queda reservado.
- **Los huecos son intencionales.** Por ejemplo, `E-CON-009` no existe; se dejó para no renumerar `E-CON-010` en adelante.
- **Cada código nuevo va al final de su familia** y se agrega a este catálogo en el mismo cambio.
- **Cada código debe tener al menos una prueba** que compare `excinfo.value.codigo`.
- **El mensaje debe decir qué pasó y qué se esperaba**, con la ruta o el campo cuando aplique.
- **No incluir secretos, tokens ni rutas fuera del workspace** en el mensaje.
- **No cambiar el significado de un código existente.** Si cambia la condición, se crea un código nuevo.
- **Las advertencias usan `W-`** y no deben impedir la publicación del archivo.

---

## 8. Mapa rápido

| Familia | Archivo | Excepción | Se lanza en |
|---|---|---|---|
| `E-CON` | `conversion.py` | `ConversionError` | Validación del `.pptx`, ejecución de `soffice`/`pdftoppm`, verificación del PDF y renombrado de miniaturas |
| `E-VER` | `verificacion.py` | `VerificacionError` | Apertura del archivo, valores esperados vacíos, formato no soportado |
| `E-PPT` | `pptx_ensamblar.py` | `PptxError` | Skill, branding, plantilla, contenido, marcadores, gráficas, tablas y guardado |
| `E-DOC` | `docx_ensamblar.py` | `DocxError` | Branding, plantilla, JSON del Copiloto, marcadores y guardado |
| `E-XLS` | `xlsx_ensamblar.py` | `XlsxError` | Branding, datos, formatos, resumen, marcadores, guardado y verificación estructural |
| `E-SKL` | `skills.py` | `SkillError` | Validación de la skill, lectura y búsqueda |
| `E-MET` | `metricas.py` | `MetricaError` | Carga de la skill, DSL, columnas, fuente de datos y consulta DuckDB |
| `E-MAR` | `marcadores.py` | `MarcadorError` | Formato, valor no numérico o marcador sin valor; se traduce a `E-PPT/DOC/XLS-030` |
