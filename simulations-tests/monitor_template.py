"""Per-scenario observer. Copy to _temp before adapting it for a change."""
import sys
from pathlib import Path

root = next(p for p in Path(__file__).resolve().parents if (p / "run.py").is_file())
sys.path.insert(0, str(root))
sys.path.insert(0, str(root.parent))
from harness.monitor import Collector


class Monitor(Collector):
    def observe(self, name, packet):
        # Example: self.trace.write("derived_brake", {"value": packet.Brake})
        # when name == "OutGaugePack". Original packet fields are always saved.
        pass


if __name__ == "__main__":
    # Works as monitor_template.py, scenarios/<name>/monitor.py or _temp/**/monitor.py.
    from run import main
    raise SystemExit(main(["monitor", "--monitor", str(Path(__file__).resolve()), *sys.argv[1:]]))
