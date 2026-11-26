"""Run from the repository root: python -m research.multi_horizon --help."""
import argparse
from pathlib import Path

from .engine import DEFAULT_PROTOCOL, run_research


def main():
    parser = argparse.ArgumentParser(description='台股多期限離線價格路徑研究；不連線券商或模型')
    parser.add_argument('--bars', type=Path, required=True, help='日線 CSV')
    parser.add_argument('--sessions', type=Path, required=True, help='交易所交易日 CSV，date 欄位')
    parser.add_argument('--protocol', type=Path, default=DEFAULT_PROTOCOL)
    parser.add_argument('--output-root', type=Path, default=Path('research-runs'))
    parser.add_argument('--dataset-note', required=True, help='資料來源、歷史股票範圍、除權息與排除規則')
    args = parser.parse_args()
    try:
        destination = run_research(args.bars, args.sessions, protocol_path=args.protocol,
                                   output_root=args.output_root, dataset_note=args.dataset_note)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.exit(2, f'研究未完成：{exc}\n')
    print(destination)


if __name__ == '__main__':
    main()
