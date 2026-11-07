# Blastproof sandbox image.
# Everything the agent's code needs is baked in at build time, because at run
# time the container has no network (no pip install, no downloads).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MPLBACKEND=Agg

RUN pip install --no-cache-dir \
        pandas==2.2.3 \
        numpy==2.2.1 \
        matplotlib==3.10.0 \
        scipy==1.15.1 \
        openpyxl==3.1.5 \
        pyarrow==18.1.0 \
        tabulate==0.9.0 \
    && python -c "import matplotlib.pyplot as plt; plt.figure(); plt.close('all')" \
    # Remove tools an attacker would love to have.
    && rm -f /usr/bin/apt* /usr/bin/dpkg* /usr/bin/curl /usr/bin/wget /bin/su /usr/bin/su \
             /usr/bin/passwd /usr/bin/chsh /usr/bin/chfn /usr/bin/newgrp /usr/bin/gpasswd

# Unprivileged user. The container also runs with --user 10001:10001.
RUN groupadd -g 10001 agent && useradd -u 10001 -g 10001 -M -d /tmp -s /usr/sbin/nologin agent

COPY runner.py /opt/blastproof/runner.py
RUN chmod 0444 /opt/blastproof/runner.py

USER 10001:10001
WORKDIR /tmp
ENTRYPOINT ["python", "-I", "/opt/blastproof/runner.py"]
