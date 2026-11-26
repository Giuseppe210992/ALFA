import json

def load_config(path):
    try:
        import yaml
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except ImportError:
        d = {}
        with open(path) as f:
            for line in f:
                line = line.split("#")[0].strip()
                if ":" in line:
                    k, v = line.split(":", 1)
                    v = v.strip()
                    try:
                        v = json.loads(v)
                    except Exception:
                        pass
                    d[k.strip()] = v
        return d
