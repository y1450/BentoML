import inspect
import logging
import os
import shutil
import tarfile
import typing as t
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import attr
import yaml
from simple_di import Provide, inject

from bentoml import __version__ as BENTOML_VERSION

from ...exceptions import BentoMLException, InvalidArgument
from ..configuration.containers import BentoMLContainer
from ..types import GenericDictType, PathType
from ..utils import generate_new_version_id, validate_or_create_dir
from . import MODEL_STORE_PREFIX, MODEL_YAML_NAMESPACE, YAML_EXT

logger = logging.getLogger(__name__)

RESERVED_MODEL_FIELD = [
    "name",
    "path",
    "version",
    "module",
    "created_at",
    "labels",
    "context",
]
SUPPORTED_COMPRESSION_TYPE = [".gz"]
MODEL_TAR_EXTENSION = ".models.tar.gz"


def validate_name(name: str):
    if not name.isidentifier():
        raise InvalidArgument(
            f"Invalid model name: '{name}'. A valid identifier "
            "may only contain letters, numbers, underscores "
            "and not starting with a number."
        )


def _generate_model_tag(name: str):
    validate_name(name)
    version = generate_new_version_id()
    return f"{name}:{version}"


def _process_model_tag(tag: str) -> (str, str):
    try:
        _name, _version = tag.split(":")
        validate_name(_name)
        return _name, _version
    except ValueError:
        validate_name(tag)
        # when name is a model name without versioning
        return tag, "latest"


@attr.s
class StoreCtx(object):
    name = attr.ib(type=str)
    version = attr.ib(type=str)
    path = attr.ib(type=PathType)
    labels = attr.ib(type=t.Dict[str, str], factory=dict, kw_only=True)
    options = attr.ib(type=GenericDictType, factory=dict, kw_only=True)
    metadata = attr.ib(type=GenericDictType, factory=dict, kw_only=True)


@attr.s
class ModelInfo(StoreCtx):
    module = attr.ib(type=str, kw_only=True)
    created_at = attr.ib(type=str, kw_only=True)
    context = attr.ib(type=GenericDictType, factory=dict)
    api_version = attr.ib(type=str, default="v1")
    bentoml_version = attr.ib(type=str, default=BENTOML_VERSION)


def dump_model_yaml(
    model_yaml: Path,
    ctx: "StoreCtx",
    *,
    framework_context: dict = None,
    module: str = __name__,
) -> None:
    info = ModelInfo(
        name=ctx.name,
        version=ctx.version,
        labels=ctx.labels,
        options=ctx.options,
        metadata=ctx.metadata,
        path=str(ctx.path),
        created_at=datetime.now().strftime("%Y/%m/%d %H:%M:%S"),
        context={} if framework_context is None else framework_context,
        module=module,
    )
    with model_yaml.open("w", encoding="utf-8") as f:
        yaml.safe_dump(attr.asdict(info), f)


def load_model_yaml(path: PathType) -> "ModelInfo":
    with Path(path, f"{MODEL_YAML_NAMESPACE}{YAML_EXT}").open(
        "r", encoding="utf-8"
    ) as f:
        info = yaml.safe_load(f)
    return ModelInfo(**info)


class LocalModelStore:
    @inject
    def __init__(self, base_dir: PathType = Provide[BentoMLContainer.bentoml_home]):
        self._BASE_DIR = Path(base_dir, MODEL_STORE_PREFIX)
        validate_or_create_dir(self._BASE_DIR)

    def list_model(self, tag: t.Optional[str] = None) -> t.List[str]:
        """
        bentoml models list -> t.List[models name under BENTOML_HOME/models]
        bentoml models list my_nlp_models -> t.List[model_version]
        """
        if not tag:
            path = self._BASE_DIR
        elif ":" not in tag:
            path = Path(self._BASE_DIR, tag)
        else:
            name, version = _process_model_tag(tag)
            path = Path(self._BASE_DIR, name, version)
            if version == "latest":
                path = path.resolve()
        return [_f.name for _f in path.iterdir()]

    def _create_path(self, tag: str):
        name, version = _process_model_tag(tag)
        model_path = Path(self._BASE_DIR, name, version)
        validate_or_create_dir(model_path)
        return model_path

    @contextmanager
    def register_model(
        self,
        name: str,
        *,
        module: str = "",
        options: GenericDictType = None,
        metadata: GenericDictType = None,
        labels: GenericDictType = None,
        framework_context: dict = None,
    ) -> "t.Iterator[StoreCtx]":
        """
        with bentoml.models.register(name, options, metadata, labels) as ctx:
            # ctx(model_path, version, metadata)
            model.save(ctx.model_path, metadata=ctx.metadata)
            ctx.metadata["params_a"] = value_a
        """
        tag = _generate_model_tag(name)
        _, version = tag.split(":")
        model_path = self._create_path(tag)
        model_yaml = Path(model_path, f"{MODEL_YAML_NAMESPACE}{YAML_EXT}")

        ctx = StoreCtx(
            name=name,
            path=model_path,
            version=version,
            labels=labels,
            metadata=metadata,
            options=options,
        )
        try:
            yield ctx
        except Exception:  # noqa
            # save has failed
            logger.warning(f"Failed to save {tag}, deleting {model_path}...")
            shutil.rmtree(model_path)
        finally:
            latest_path = Path(self._BASE_DIR, name, "latest")
            dump_model_yaml(
                model_yaml, ctx, framework_context=framework_context, module=module
            )
            if latest_path.is_symlink():
                latest_path.unlink()
            latest_path.symlink_to(model_path)

    def get_model(self, tag: str) -> "ModelInfo":
        """
        bentoml.pytorch.get("my_nlp_model")
        """
        name, version = _process_model_tag(tag)
        path = Path(self._BASE_DIR, name, version)
        if not path.exists():
            raise FileNotFoundError(
                f"Model '{tag}' is not found under BentoML modelstore {self._BASE_DIR}."
            )
        return load_model_yaml(path)

    def delete_model(self, tag: str, skip_confirm: bool = False):
        """
        bentoml models delete
        """
        model_name, version = _process_model_tag(tag)
        basepath = Path(self._BASE_DIR, model_name)
        try:
            if ":" not in tag:
                basepath.rmdir()
            else:
                path = Path(basepath, version)
                path.rmdir()
                path.unlink(missing_ok=True)
        finally:
            indexed = sorted(basepath.iterdir(), key=os.path.getctime)
            latest_path = Path(basepath, "latest")
            if latest_path.is_symlink():
                latest_path.unlink()
            latest_path.symlink_to(indexed[-1])

    def push_model(self, tag: str):
        ...

    def pull_model(self, tag: str):
        ...

    def export_model(self, tag: str, exported_path: PathType) -> None:
        model_info = self.get_model(tag)
        fname = f"{model_info.name}_{model_info.version}{MODEL_TAR_EXTENSION}"
        with tarfile.open(os.path.join(exported_path, fname), mode="w:gz") as tfile:
            tfile.add(str(model_info.path), arcname="")

    def import_model(self, path: PathType, override=False) -> str:
        _path_obj = Path(path)
        if _path_obj.suffix not in SUPPORTED_COMPRESSION_TYPE:
            raise BentoMLException(
                f"Compression type from {path} is not yet supported. "
                f"Currently supports: {SUPPORTED_COMPRESSION_TYPE}."
            )
        try:
            with tarfile.open(path, mode="r:gz") as tfile:
                with tfile.extractfile(
                    f"{MODEL_YAML_NAMESPACE}{YAML_EXT}"
                ) as model_yaml:
                    model_info = ModelInfo(**yaml.safe_load(model_yaml))
                    target = Path(self._BASE_DIR, model_info.name, model_info.version)
                    validate_or_create_dir(target)
                    if not override and any(target.iterdir()):
                        raise FileExistsError
                tfile.extractall(path=str(target))
            return str(target)
        except FileExistsError:
            tag = f"{model_info.name}:{model_info.version}"
            model_info = self.get_model(tag)
            _LOAD_INST = """\
            import {module}
            model = {module}.load("{name}", **kwargs)
            """
            _LOAD_RUNNER_INST = """\
            import {module}
            runner = {module}.load_runner("{name}", **kwargs)
            """
            raise FileExistsError(
                f"Model `{tag}` have already been imported.\n"
                f"Import the model directly with `load`:\n\n"
                f"{inspect.cleandoc(_LOAD_INST.format(module=model_info.module, name=tag))}\n\n"
                f"Use runner directly with `load_runner`:\n\n"
                f"{inspect.cleandoc(_LOAD_RUNNER_INST.format(module=model_info.module, name=tag))}\n\n"
                f"If one wants to override, do\n\nbentoml.models.imports('{path}', override=True)"
            )


# Global modelstore instance
modelstore = LocalModelStore()

ls = modelstore.list_model
register = modelstore.register_model
delete = modelstore.delete_model
get = modelstore.get_model
export = modelstore.export_model
imports = modelstore.import_model