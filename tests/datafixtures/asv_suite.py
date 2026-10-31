from pathlib import Path

from narrow.asv import SavedRunBenchmark, read_results

class Http(SavedRunBenchmark):
    params = [1, 4]

    def setup_cache(self) -> dict[int, dict[str, float]]:
        return read_results(Path(__file__).with_name("run.json"), stand="uwsgi", app="py")
