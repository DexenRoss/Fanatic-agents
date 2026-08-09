# Fanatic Agents

Fanatic Agents es una plataforma experimental para orquestar agentes de IA especializados en proyectos de software con permisos explícitos y autonomía limitada. Sprint 1 añade inspección determinística de repositorios y un Developer Agent opcional de solo lectura.


## Requisitos

- Python 3.12 o posterior
- `pip`
- Git, Docker y GitHub CLI (`gh`) son recomendables y se comprueban con `doctor`
- `OPENAI_API_KEY` solo es necesaria para la opción `inspect --ai`

## Instalación

En Linux o macOS:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e .
```

En PowerShell:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e .
```

Para desarrollo y ejecución de tests, instala el extra `dev`:

```bash
pip install -e ".[dev]"
```

## Uso

Consulta los comandos disponibles:

```bash
fanatic-agents --help
```

Diagnostica el entorno local sin efectuar llamadas de red ni mostrar secretos:

```bash
fanatic-agents doctor
```

Valida una configuración de proyecto:

```bash
fanatic-agents config validate projects/example.yaml
```

### Inspección de repositorios

La inspección local es determinística y no usa OpenAI:

```bash
fanatic-agents inspect /ruta/al/repositorio
```

El inspector acepta repositorios Git y directorios de proyecto sin Git. Detecta tecnologías, archivos importantes y posibles comandos de test/build, pero nunca ejecuta esos comandos. Incluye una selección pequeña y determinística de entry points y módulos fuente arquitectónicamente relevantes. Filtra secretos, binarios, dependencias, artefactos generados y symlinks; el contenido incluido queda acotado por límites de archivos fuente, por archivo y por snapshot.

Para solicitar una única evaluación estructurada del Developer Agent:

```bash
export OPENAI_API_KEY="..."
fanatic-agents inspect /ruta/al/repositorio --ai
```

`--ai` puede generar costos de API. En Sprint 1 el agente es de solo lectura, no tiene tools, recibe únicamente el snapshot filtrado, no ejecuta código y no modifica archivos del repositorio analizado. Sin `--ai`, no se requiere `OPENAI_API_KEY` y no existe ninguna llamada a OpenAI.

El modelo es opcional; si no se configura, se usa el default actual del Agents SDK:

```bash
export FANATIC_AGENTS_MODEL="..."
```

Ejecuta la suite de tests:

```bash
python -m pytest
```

### Sandbox Docker experimental

Comprueba que la CLI de Docker esta instalada y que el daemon responde:

```bash
fanatic-agents sandbox check
```

Ejecuta manualmente un comando permitido:

```bash
fanatic-agents sandbox run . \
  --image python:3.12-slim \
  --command "python --version"
```

La imagen debe existir localmente. Fanatic Agents nunca ejecuta `docker pull`: debes descargar o construir la imagen explicitamente antes de usarla.

El sandbox trabaja exclusivamente sobre una copia temporal, filtrada y acotada del repositorio. El repositorio original no se monta en el contenedor y no recibe los cambios efectuados durante la ejecucion. Se excluyen `.git`, entornos virtuales, dependencias, artefactos generados, symlinks, binarios y nombres de archivo que puedan contener credenciales, incluidos `.env`, `.env.*`, claves y tokens.

La red permanece deshabilitada. El contenedor usa limites conservadores de tiempo, memoria, CPU, procesos y salida; elimina capabilities, activa `no-new-privileges`, usa un root filesystem de solo lectura y recibe unicamente `HOME=/tmp` y `PYTHONDONTWRITEBYTECODE=1`. No se montan sockets, configuraciones personales ni credenciales, y `OPENAI_API_KEY` no se pasa al contenedor.

Esta funcionalidad es experimental. El Developer Agent continua sin tools y no tiene acceso al sandbox; Sprint 2 solo permite invocarlo manualmente desde la CLI o la API Python.


## Configuración

Las configuraciones YAML se validan estrictamente: no se aceptan campos desconocidos, tipos implícitos incorrectos ni límites menores o iguales que cero. Consulta [`projects/example.yaml`](projects/example.yaml) como referencia.

## Política de seguridad

Fanatic Agents nunca debe realizar automáticamente las siguientes acciones sin autorización humana explícita:

- merge a la rama principal;
- deploy a producción;
- modificación de secrets;
- operaciones destructivas de base de datos.

Sus permisos tienen defaults conservadores y esas cuatro operaciones peligrosas permanecen desactivadas salvo configuración explícita. El Developer Agent de Sprint 1 no recibe herramientas y no puede modificar el repositorio.

