# Fanatic Agents

Fanatic Agents será una plataforma para orquestar agentes de IA especializados en proyectos de software. El proyecto está en **Sprint 0 / estado experimental**: actualmente ofrece una base modular, modelos estrictos de configuración y una CLI de diagnóstico. Todavía no ejecuta agentes ni realiza llamadas a OpenAI.

## Requisitos

- Python 3.12 o posterior
- `pip`
- Git, Docker y GitHub CLI (`gh`) son recomendables y se comprueban con `doctor`
- `OPENAI_API_KEY` será necesaria en sprints futuros, pero Sprint 0 no la usa

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

Ejecuta la suite de tests:

```bash
python -m pytest
```

## Configuración

Las configuraciones YAML se validan estrictamente: no se aceptan campos desconocidos, tipos implícitos incorrectos ni límites menores o iguales que cero. Consulta [`projects/example.yaml`](projects/example.yaml) como referencia.

## Política de seguridad

Fanatic Agents nunca debe realizar automáticamente las siguientes acciones sin autorización humana explícita:

- merge a la rama principal;
- deploy a producción;
- modificación de secrets;
- operaciones destructivas de base de datos.

Sus permisos tienen defaults conservadores y esas cuatro operaciones peligrosas permanecen desactivadas salvo configuración explícita. Los mecanismos de aprobación y la ejecución de estas operaciones están fuera del alcance del Sprint 0.

