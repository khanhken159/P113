OPTIMIZER AGENT CODE BUNDLE

Goi nay chua ma nguon optimizer giu nguyen cau truc module src/optimize_agent, bo SQL mau va cau hinh DuckDB. Khong chua .env, API key hay database benchmark.

Chay benchmark TPC-H trong PowerShell (Python 3.11 khuyen nghi):
  python -m venv .venv
  .\.venv\Scripts\Activate.ps1
  python -m pip install -r requirements-optimizer.txt
  python -m src.optimize_agent.cli setup --dataset tpch
  python -m src.optimize_agent.cli optimize --dataset tpch --k 1
  python -m src.optimize_agent.cli eval

Bao cao nam trong reports/. Cau hinh mac dinh tat LLM, khong can API key.

Luu y: goi nay chay benchmark mau. De toi uu du lieu rieng, can them loader cho nguon du lieu va ModelSpec/SQL tham chieu cua ban trong src/optimize_agent/datasets; chi copy code khong tu dong ket noi toi data rieng.
