"""MediaFlux 源码与冻结构建的统一入口。"""
from app.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
