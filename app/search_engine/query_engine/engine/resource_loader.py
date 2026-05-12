from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml


class ResourceLoaderError(Exception):
    """Base exception for resource loading failures."""


class ResourceNotFoundError(ResourceLoaderError):
    """Raised when a required resource directory or file does not exist."""


class InvalidResourceError(ResourceLoaderError):
    """Raised when a resource file is malformed or has an unexpected type."""


@dataclass(slots=True)
class LocaleBundle:
    """
    Normalized, merged resource bundle used by the query analyzer.

    Attributes:
        primary_locale: Main locale selected by the caller.
        active_locales: Ordered locales used for loading. `common` is excluded.
        common: Raw merged common resources by filename stem.
        locales: Raw per-locale resources by locale -> filename stem -> content.
        merged: Final merged resources by filename stem.
    """

    primary_locale: str
    active_locales: list[str]
    common: dict[str, Any] = field(default_factory=dict)
    locales: dict[str, dict[str, Any]] = field(default_factory=dict)
    merged: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.merged.get(key, default)

    def require(self, key: str) -> Any:
        if key not in self.merged:
            raise KeyError(f"Resource '{key}' is not loaded.")
        return self.merged[key]


class ResourceLoader:
    """
    Loads resource files from a directory structure like:

        resources/
        ├─ common/
        │  ├─ model_scopes.yaml
        │  ├─ field_aliases.yaml
        │  └─ ...
        ├─ ko/
        │  ├─ time.yaml
        │  ├─ operators.yaml
        │  └─ ...
        └─ en/
           ├─ time.yaml
           ├─ operators.yaml
           └─ ...

    Design goals:
    - Always load `common/` first.
    - Load one or more locale directories in order.
    - Merge resources by filename stem.
    - Later locale entries override earlier ones only at the leaf level.
    - Keep raw common/locale snapshots for debugging.
    """

    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = Path(base_dir)
        if not self.base_dir.exists() or not self.base_dir.is_dir():
            raise ResourceNotFoundError(
                f"Resource base directory does not exist: {self.base_dir}"
            )

    def list_locales(self) -> list[str]:
        locales: list[str] = []
        for child in sorted(self.base_dir.iterdir()):
            if child.is_dir() and child.name != "common":
                locales.append(child.name)
        return locales

    def list_resource_files(self, locale: str) -> list[str]:
        locale_dir = self.base_dir / locale
        if not locale_dir.exists() or not locale_dir.is_dir():
            raise ResourceNotFoundError(f"Locale directory does not exist: {locale_dir}")
        return sorted(p.name for p in locale_dir.glob("*.yaml"))

    def load_bundle(
        self,
        *,
        primary_locale: str,
        active_locales: Iterable[str] | None = None,
        required_common_files: Iterable[str] | None = None,
        required_locale_files: Iterable[str] | None = None,
    ) -> LocaleBundle:
        """
        Load and merge common + locale resources.

        Args:
            primary_locale: Main locale, e.g. 'ko'.
            active_locales: Ordered locale list. If omitted, only primary_locale is used.
            required_common_files: File stems that must exist in common/.
            required_locale_files: File stems that must exist in each active locale.

        Returns:
            LocaleBundle
        """
        normalized_locales = self._normalize_active_locales(
            primary_locale=primary_locale,
            active_locales=active_locales,
        )

        common_resources = self._load_directory_resources(
            self.base_dir / "common",
            required_file_stems=set(required_common_files or []),
        )

        per_locale: dict[str, dict[str, Any]] = {}
        merged: dict[str, Any] = self._deep_copy(common_resources)

        for locale in normalized_locales:
            locale_resources = self._load_directory_resources(
                self.base_dir / locale,
                required_file_stems=set(required_locale_files or []),
            )
            per_locale[locale] = locale_resources
            merged = self._merge_resource_groups(merged, locale_resources)

        return LocaleBundle(
            primary_locale=primary_locale,
            active_locales=normalized_locales,
            common=common_resources,
            locales=per_locale,
            merged=merged,
        )

    def load_single_resource(
        self,
        *,
        locale: str,
        resource_name: str,
    ) -> Any:
        """Load one YAML resource by locale and resource stem."""
        file_path = self.base_dir / locale / f"{resource_name}.yaml"
        return self._read_yaml_file(file_path)

    def _normalize_active_locales(
        self,
        *,
        primary_locale: str,
        active_locales: Iterable[str] | None,
    ) -> list[str]:
        if active_locales is None:
            locales = [primary_locale]
        else:
            locales = list(active_locales)
            if primary_locale not in locales:
                locales.insert(0, primary_locale)

        normalized: list[str] = []
        seen: set[str] = set()
        for locale in locales:
            if locale == "common":
                continue
            if locale in seen:
                continue
            seen.add(locale)
            normalized.append(locale)

        for locale in normalized:
            locale_dir = self.base_dir / locale
            if not locale_dir.exists() or not locale_dir.is_dir():
                raise ResourceNotFoundError(
                    f"Active locale directory does not exist: {locale_dir}"
                )

        return normalized

    def _load_directory_resources(
        self,
        directory: Path,
        *,
        required_file_stems: set[str],
    ) -> dict[str, Any]:
        if not directory.exists() or not directory.is_dir():
            raise ResourceNotFoundError(f"Resource directory does not exist: {directory}")

        resources: dict[str, Any] = {}
        for file_path in sorted(directory.glob("*.yaml")):
            resources[file_path.stem] = self._read_yaml_file(file_path)

        missing = required_file_stems - set(resources.keys())
        if missing:
            raise ResourceNotFoundError(
                f"Missing required resource files in {directory}: {sorted(missing)}"
            )

        return resources

    def _read_yaml_file(self, file_path: Path) -> Any:
        if not file_path.exists() or not file_path.is_file():
            raise ResourceNotFoundError(f"Resource file does not exist: {file_path}")

        try:
            with file_path.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except yaml.YAMLError as exc:
            raise InvalidResourceError(f"Invalid YAML in {file_path}: {exc}") from exc
        except OSError as exc:
            raise ResourceLoaderError(f"Failed to read resource file {file_path}: {exc}") from exc

        if data is None:
            return {}
        if not isinstance(data, (dict, list, str, int, float, bool)):
            raise InvalidResourceError(
                f"Unsupported YAML root type in {file_path}: {type(data).__name__}"
            )
        return data

    def _merge_resource_groups(
        self,
        base_group: dict[str, Any],
        override_group: dict[str, Any],
    ) -> dict[str, Any]:
        result = self._deep_copy(base_group)
        for resource_name, override_value in override_group.items():
            if resource_name not in result:
                result[resource_name] = self._deep_copy(override_value)
                continue
            result[resource_name] = self._deep_merge(result[resource_name], override_value)
        return result

    def _deep_merge(self, base: Any, override: Any) -> Any:
        """
        Merge rule:
        - dict + dict -> recursive merge
        - list + list -> concatenated unique-preserving merge
        - scalar/other -> override wins
        """
        if isinstance(base, dict) and isinstance(override, dict):
            merged = {k: self._deep_copy(v) for k, v in base.items()}
            for key, value in override.items():
                if key in merged:
                    merged[key] = self._deep_merge(merged[key], value)
                else:
                    merged[key] = self._deep_copy(value)
            return merged

        if isinstance(base, list) and isinstance(override, list):
            merged_list: list[Any] = []
            for item in [*base, *override]:
                if item not in merged_list:
                    merged_list.append(self._deep_copy(item))
            return merged_list

        return self._deep_copy(override)

    def _deep_copy(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {k: self._deep_copy(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._deep_copy(v) for v in value]
        return value


if __name__ == "__main__":
    # Example usage
    # resources/ 폴더가 engine/ 폴더보다 한 단계 위에 있으므로 parent.parent 사용
    loader = ResourceLoader(base_dir=Path(__file__).parent.parent / "resources")
    bundle = loader.load_bundle(
        primary_locale="ko",
        active_locales=["ko", "en"],
        required_common_files=["model_scope", "field_alias", "operators", "units", "file_types"],
        required_locale_files=[], # 현재 사용자가 만든 yaml 구조가 조금 다르므로 임시로 비워서 테스트
    )

    print("Primary locale:", bundle.primary_locale)
    print("Active locales:", bundle.active_locales)
    print("Merged resource keys:", sorted(bundle.merged.keys()))
    print("Operators:", bundle.get("operators", {}))
