#!/usr/bin/env bash
set -euo pipefail

# Patch Airflow to install hvb-specific requirements on startup / Patch Airflow để cài requirements riêng của hvb khi khởi động

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"

# Prefer project kubeconfig first / Ưu tiên kubeconfig của project
if [ -n "${K3S_CONF:-}" ]; then
  export KUBECONFIG="$K3S_CONF"
elif [ -f "$DIR/config.k3s-new" ]; then
  export KUBECONFIG="$DIR/config.k3s-new"
elif [ -f "$HOME/.kube/config.k3s-new" ]; then
  export KUBECONFIG="$HOME/.kube/config.k3s-new"
fi

NAMESPACE="orchestrator"
HVB_REQ="/opt/airflow/dags/hvb-processing/requirements.txt"

SCHEDULER_CMD='if [ -f /opt/airflow/dags/requirements.txt ]; then python3 -m pip install -r /opt/airflow/dags/requirements.txt; fi && if [ -f /opt/airflow/dags/hvb-processing/requirements.txt ]; then python3 -m pip install -r /opt/airflow/dags/hvb-processing/requirements.txt; fi && python3 -m pip install apache-airflow-providers-postgres==6.0.0 apache-airflow-providers-neo4j==3.7.0 apache-airflow-providers-apache-spark==4.11.0 && airflow scheduler'
WEBSERVER_CMD='if [ -f /opt/airflow/dags/requirements.txt ]; then python3 -m pip install -r /opt/airflow/dags/requirements.txt; fi && if [ -f /opt/airflow/dags/hvb-processing/requirements.txt ]; then python3 -m pip install -r /opt/airflow/dags/hvb-processing/requirements.txt; fi && python3 -m pip install apache-airflow-providers-postgres==6.0.0 apache-airflow-providers-neo4j==3.7.0 apache-airflow-providers-apache-spark==4.11.0 && airflow webserver'

echo "Patching scheduler command to include ${HVB_REQ}"
kubectl patch statefulset airflow-lab-scheduler -n "$NAMESPACE" --type strategic --patch "$(cat <<EOF
spec:
  template:
    spec:
      containers:
      - name: scheduler
        command:
        - /bin/bash
        - -c
        - ${SCHEDULER_CMD}
EOF
)"

echo "Patching webserver command to include ${HVB_REQ}"
kubectl patch deployment airflow-lab-webserver -n "$NAMESPACE" --type strategic --patch "$(cat <<EOF
spec:
  template:
    spec:
      containers:
      - name: webserver
        command:
        - /bin/bash
        - -c
        - ${WEBSERVER_CMD}
EOF
)"

echo "Waiting for rollouts..."
kubectl rollout status statefulset/airflow-lab-scheduler -n "$NAMESPACE" --timeout=180s
kubectl rollout status deployment/airflow-lab-webserver -n "$NAMESPACE" --timeout=180s

echo "Done. Airflow now installs hvb requirements from ${HVB_REQ}."
