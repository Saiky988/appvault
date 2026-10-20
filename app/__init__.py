"""AppVault — Self-hosted application catalog tracker, FastAPI service, and Discord bot."""
from __future__ import annotations

import pydantic

__version__ = "1.0.0"

# Seamless runtime compatibility if executed on Pydantic < 2.0
if not hasattr(pydantic, "ConfigDict"):
    def _config_dict(**kwargs: object) -> dict[str, object]:
        return dict(kwargs)

    pydantic.ConfigDict = _config_dict  # type: ignore[attr-defined]

    if hasattr(pydantic, "main") and hasattr(pydantic.main, "ModelMetaclass"):
        _orig_meta_new = pydantic.main.ModelMetaclass.__new__

        def _compat_meta_new(mcs, name, bases, namespace, **kwargs):  # type: ignore[no-untyped-def]
            mc = namespace.pop("model_config", None)
            if isinstance(mc, dict) and "Config" not in namespace:
                class Config:
                    orm_mode = mc.get("from_attributes", True)
                    extra = mc.get("extra", "ignore")
                    allow_population_by_field_name = mc.get("populate_by_name", True)

                namespace["Config"] = Config
            cls = _orig_meta_new(mcs, name, bases, namespace, **kwargs)
            if mc is not None:
                cls._v2_model_config = mc
            return cls

        pydantic.main.ModelMetaclass.__new__ = _compat_meta_new  # type: ignore[method-assign]

if not hasattr(pydantic, "field_validator"):
    def _field_validator(*fields: str, mode: str = "after", **_kwargs: object):  # type: ignore[no-untyped-def]
        return pydantic.validator(*fields, pre=(mode == "before"), allow_reuse=True)

    pydantic.field_validator = _field_validator  # type: ignore[attr-defined]

if not hasattr(pydantic.BaseModel, "model_dump"):
    pydantic.BaseModel.model_dump = lambda self, *a, **kw: self.dict(*a, **kw)  # type: ignore[attr-defined]

if not hasattr(pydantic.BaseModel, "model_dump_json"):
    pydantic.BaseModel.model_dump_json = lambda self, *a, **kw: self.json(*a, **kw)  # type: ignore[attr-defined]

if not hasattr(pydantic.BaseModel, "model_validate"):
    pydantic.BaseModel.model_validate = classmethod(  # type: ignore[attr-defined]
        lambda cls, obj, *a, **kw: obj if isinstance(obj, cls) else (cls.parse_obj(obj) if isinstance(obj, dict) else cls.from_orm(obj))
    )
