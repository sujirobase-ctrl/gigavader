"""Use a sandboxed Android JS interpreter where Windows uses bundled Node."""
import re
import src.resolvers as resolvers


def install(runtime):
    def discover(client, force=False):
        if resolvers._gofile_salt_cache and not force:
            return resolvers._gofile_salt_cache
        try:
            with client.get(resolvers.GOFILE_WT_SCRIPT, stream=True, timeout=(5, 10)) as response:
                response.raise_for_status()
                data = bytearray()
                for chunk in response.iter_content(32768):
                    data.extend(chunk)
                    if len(data) > 2 * 1024 * 1024: raise ValueError('Script demasiado grande')
            parts = str(runtime.evaluate(data.decode('utf-8'))).strip().split('::')
            if len(parts) >= 5 and re.fullmatch(r'[\w-]{6,64}', parts[-1]):
                resolvers._gofile_salt_cache = parts[-1]
                return parts[-1]
        except Exception:
            pass
        resolvers._gofile_salt_cache = resolvers.GOFILE_DEFAULT_SALT
        return resolvers._gofile_salt_cache
    resolvers._discover_gofile_salt = discover
