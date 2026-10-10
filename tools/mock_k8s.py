"""
K8s MCP 工具的离线 mock 实现。

存在的理由：mcp-k8s-eye 部署在虚拟机上，虚拟机没开机时 Agent 连不上 MCP Server。
本模块用同样的工具名与入参 schema 伪造一份返回，让 Agent 主逻辑、对话记忆、CLI
界面可以在离线状态下先跑通；等虚拟机开机，把 ALLOW_MCP_FALLBACK 改为 false 即可
切回真实工具，业务代码一行不用改。

数据刻意复刻需求文档 §1.4 的演示场景：单节点集群、kube-system 下 7 个 Pod、
两个 CoreDNS 异常、Flannel 写入失败。便于把离线输出和 PDF 里的效果做对照。

注意：真实工具名含空格（如 "resource list"），这里刻意保持一模一样，让 mock 与
真机接口完全对齐；暴露给 LLM 前的名称净化（空格→下划线）由 agent.py 的
_prepare_tools 统一处理，两条路径走同一套逻辑。
"""

from __future__ import annotations

from langchain_core.tools import tool

# 与真实 MCP Server 完全一致的入参（含必填项），来自 mcp-k8s-eye/pkg/mcp/*.go
_NODE = "master"

_CLUSTER_NODES = [
    {"name": "master", "status": "Ready", "roles": "control-plane", "version": "v1.35.2",
     "internal_ip": "192.168.1.189", "os": "Ubuntu 24.04.4 LTS", "kernel": "6.8.0-100-generic",
     "container_runtime": "containerd://1.7.28", "cpu": "4", "memory": "7445040Ki",
     "labels": {"kubernetes.io/hostname": "master", "kubernetes.io/os": "linux",
                "kubernetes.io/arch": "amd64", "node-role.kubernetes.io/control-plane": "",
                "node-role.kubernetes.io/gpu": "huawei"}},
]

_CLUSTER_PODS = [
    {"name": "coredns-bbdc5fdf6-srwkq", "namespace": "kube-system", "node": _NODE,
     "phase": "Running", "ready": "0/1", "restarts": 0, "issue": "DNS 服务未就绪"},
    {"name": "coredns-bbdc5fdf6-wpj48", "namespace": "kube-system", "node": _NODE,
     "phase": "Running", "ready": "0/1", "restarts": 0, "issue": "DNS 服务未就绪"},
    {"name": "etcd-master", "namespace": "kube-system", "node": _NODE,
     "phase": "Running", "ready": "1/1", "restarts": 3, "issue": None},
    {"name": "kube-apiserver-master", "namespace": "kube-system", "node": _NODE,
     "phase": "Running", "ready": "1/1", "restarts": 3, "issue": None},
    {"name": "kube-controller-manager-master", "namespace": "kube-system", "node": _NODE,
     "phase": "Running", "ready": "1/1", "restarts": 3, "issue": None},
    {"name": "kube-proxy-gtk8j", "namespace": "kube-system", "node": _NODE,
     "phase": "Running", "ready": "1/1", "restarts": 2, "issue": None},
    {"name": "kube-scheduler-master", "namespace": "kube-system", "node": _NODE,
     "phase": "Running", "ready": "1/1", "restarts": 3, "issue": None},
]

_FLANNEL_ERROR = (
    "Failed to write to /proc/sys/net/bridge/bridge-nf-call-iptables: "
    "open /proc/sys/net/bridge/bridge-nf-call-iptables: no such file or directory"
)


def _mock_header() -> str:
    return "【MOCK 数据】以下内容由本地 mock 工具生成，未连接真实集群。\n"


@tool("node analyze")
def node_analyze(name: str = "") -> str:
    """analyze node status - 分析节点状态（mock）。不传 name 时返回全部节点。"""
    targets = [n for n in _CLUSTER_NODES if not name or n["name"] == name] or _CLUSTER_NODES
    lines = [_mock_header()]
    for n in targets:
        lines.append(f"节点: {n['name']}  状态: {n['status']}  角色: {n['roles']}")
        lines.append(f"  内部 IP: {n['internal_ip']}  主机名: {n['name']}")
        lines.append(f"  操作系统: {n['os']}  内核: {n['kernel']}")
        lines.append(f"  容器运行时: {n['container_runtime']}  Kubelet: {n['version']}")
        lines.append(f"  容量: CPU {n['cpu']} cores / 内存 {n['memory']}")
        lines.append("  状态条件: Ready=True  MemoryPressure=False  DiskPressure=False  "
                     "PIDPressure=False  NetworkUnavailable=False")
        lines.append("  标签: " + ", ".join(f"{k}={v or '(empty)'}" for k, v in n["labels"].items()))
        lines.append("  提示: 集群只有 1 台节点，属单节点部署；如需高可用建议至少再加 2-3 个工作节点。")
    return "\n".join(lines)


@tool("pod analyze")
def pod_analyze(namespace: str = "") -> str:
    """analyze pod - 分析命名空间下的 Pod 状态（mock）。"""
    pods = [p for p in _CLUSTER_PODS if not namespace or p["namespace"] == namespace]
    if not pods:
        return f"{_mock_header()}命名空间 {namespace or '(全部)'} 下没有 Pod。"
    lines = [_mock_header(), f"命名空间 {namespace} 共 {len(pods)} 个 Pod："]
    for p in pods:
        flag = "异常" if p["issue"] else "正常"
        lines.append(f"  {p['name']}  Ready={p['ready']}  重启={p['restarts']}  "
                     f"状态={p['phase']}  节点={p['node']}  [{flag}]")
        if p["issue"]:
            lines.append(f"      问题: {p['issue']}")
            lines.append(f"      近 5 分钟日志: {_FLANNEL_ERROR}")
    return "\n".join(lines)


@tool("pod logs")
def pod_logs(namespace: str, pod: str) -> str:
    """get pod logs - 获取 Pod 日志（mock）。"""
    return (f"{_mock_header()}Pod {namespace}/{pod} 最近日志：\n"
            f"E0101 00:00:00.000000       1 reflector.go:147] Failed to watch *v1.Service: "
            f"failed to list *v1.Service: Get \"https://10.96.0.1:443/api/v1/services\": "
            f"dial tcp 10.96.0.1:443: i/o timeout\n"
            f"W0101 00:00:05.000000       1 plugin/kubernetes/controller.go:1] "
            f"Failed to watch *v1.Service: {_FLANNEL_ERROR}")


@tool("pod exec")
def pod_exec(namespace: str, pod: str, command: str) -> str:
    """execute command in pod - 在容器内执行命令（mock，写操作）。"""
    return f"{_mock_header()}已在 {namespace}/{pod} 执行: {command}\n(mock 不产生真实副作用)"


@tool("resource list")
def resource_list(kind: str, namespace: str) -> str:
    """list resources in a namespace - 列出某类资源（mock）。"""
    kind_l = kind.lower()
    if kind_l in ("node", "nodes"):
        rows = ["NAME     STATUS   ROLES            AGE    VERSION\n"
                "master   Ready    control-plane    30d    v1.35.2"]
    elif kind_l in ("pod", "pods"):
        rows = [f"{'NAME':<36}{'READY':<8}{'STATUS':<10}{'RESTARTS':<10}NODE"]
        rows += [f"{p['name']:<36}{p['ready']:<8}{p['phase']:<10}{p['restarts']:<10}{p['node']}"
                 for p in _CLUSTER_PODS if p["namespace"] == namespace]
        if len(rows) == 1:
            rows.append(f"(命名空间 {namespace} 下没有 Pod)")
    elif kind_l in ("deployment", "deployments"):
        rows = ["NAME    READY   UP-TO-DATE   AVAILABLE   AGE",
                f"(命名空间 {namespace} 下没有 Deployment)"]
    else:
        rows = [f"(mock 未覆盖 kind={kind}，请接入真实 MCP Server 后重试)"]
    return _mock_header() + f"命名空间: {namespace}  kind: {kind}\n" + "\n".join(rows)


@tool("resource get")
def resource_get(kind: str, namespace: str, name: str) -> str:
    """get resource details - 获取资源详情（mock）。"""
    if kind.lower() in ("node", "nodes"):
        return node_analyze.invoke({"name": name})
    for p in _CLUSTER_PODS:
        if p["name"] == name:
            return (f"{_mock_header()}kind={kind} namespace={namespace} name={name}\n"
                    f"status: {p['phase']}  ready: {p['ready']}  restarts: {p['restarts']}\n"
                    f"node: {p['node']}  issue: {p['issue'] or '无'}")
    return f"{_mock_header()}未找到 {kind}/{name}（namespace={namespace}）"


@tool("resource describe")
def resource_describe(kind: str, namespace: str, name: str) -> str:
    """describe resource - 输出资源的 describe 结果（mock）。"""
    return (f"{_mock_header()}Name: {name}\nNamespace: {namespace}\nKind: {kind}\n"
            f"Status: Running\nEvents:\n"
            f"  Warning  Unhealthy  pod/{name}  Readiness probe failed: HTTP probe failed with statuscode: 500\n"
            f"  Warning  BackOff    pod/{name}  Back-off restarting failed container")


@tool("resource create or update")
def resource_create_or_update(resource: str) -> str:
    """create or update resource - 创建或更新资源，入参为 YAML（mock，写操作）。"""
    head = resource.strip().splitlines()[0] if resource.strip() else "(空)"
    return f"{_mock_header()}已提交资源定义（首行: {head}）。mock 不产生真实副作用。"


@tool("resource delete")
def resource_delete(kind: str, namespace: str, name: str) -> str:
    """delete resource - 删除资源（mock，写操作）。"""
    return f"{_mock_header()}已删除 {kind}/{name}（namespace={namespace}）。mock 不产生真实副作用。"


@tool("deployment analyze")
def deployment_analyze(namespace: str = "") -> str:
    """analyze deployment status - 分析 Deployment（mock）。"""
    return f"{_mock_header()}命名空间 {namespace or '(全部)'} 下没有 Deployment，无需分析。"


@tool("deployment scale")
def deployment_scale(namespace: str, deployment: str, replicas: int) -> str:
    """scale deployment replicas - 调整副本数（mock，写操作）。"""
    return (f"{_mock_header()}已将 {namespace}/{deployment} 副本数调整为 {replicas}。"
            f"mock 不产生真实副作用。")


@tool("statefulset analyze")
def statefulset_analyze(namespace: str = "") -> str:
    """analyze statefulset - 分析 StatefulSet（mock）。"""
    return f"{_mock_header()}命名空间 {namespace or '(全部)'} 下没有 StatefulSet。"


@tool("service analyze")
def service_analyze(namespace: str = "") -> str:
    """analyze service - 分析 Service（mock）。"""
    return (f"{_mock_header()}命名空间 {namespace or '(全部)'} 的 Service：\n"
            f"  kubernetes  ClusterIP  10.96.0.1  <none>  443/TCP  （默认 API Server 服务）")


@tool("ingress analyze")
def ingress_analyze(namespace: str = "") -> str:
    """analyze ingress - 分析 Ingress（mock）。"""
    return f"{_mock_header()}命名空间 {namespace or '(全部)'} 下没有 Ingress。"


@tool("networkpolicy analyze")
def networkpolicy_analyze(namespace: str) -> str:
    """analyze networkpolicy - 分析 NetworkPolicy（mock）。"""
    return (f"{_mock_header()}命名空间 {namespace} 下没有 NetworkPolicy，"
            f"该命名空间内 Pod 之间流量不受限制。")


@tool("cronjob analyze")
def cronjob_analyze(namespace: str) -> str:
    """analyze cronjob - 分析 CronJob（mock）。"""
    return f"{_mock_header()}命名空间 {namespace} 下没有 CronJob。"


@tool("mutatingwebhook analyze")
def mutatingwebhook_analyze(name: str = "") -> str:
    """analyze mutating webhook - 分析 MutatingWebhookConfiguration（mock）。

    真实工具还支持 label-selector 过滤（参数名带连字符，Python 函数签名放不下），
    离线联调一般用不到，这里只保留 name。
    """
    return f"{_mock_header()}未匹配到 MutatingWebhookConfiguration（name={name or '*'}）"


@tool("validatingwebhook analyze")
def validatingwebhook_analyze(name: str = "") -> str:
    """analyze validating webhook - 分析 ValidatingWebhookConfiguration（mock）。"""
    return f"{_mock_header()}未匹配到 ValidatingWebhookConfiguration（name={name or '*'}）"


@tool("workload resource usage")
def workload_resource_usage(kind: str, namespace: str, name: str = "") -> str:
    """get workload resource usage - 查看工作负载资源用量（mock）。"""
    return (f"{_mock_header()}kind={kind} namespace={namespace} name={name or '(全部)'}\n"
            f"  CPU 请求 250m / 上限 500m    内存请求 128Mi / 上限 256Mi\n"
            f"  当前用量: CPU 12m (4.8% of request)  内存 34Mi (26.5% of request)")


def load_mock_tools() -> list:
    """返回全部 19 个 mock 工具，与真实 MCP Server 的工具集一一对应。"""
    return [
        node_analyze, pod_analyze, pod_logs, pod_exec,
        resource_list, resource_get, resource_describe,
        resource_create_or_update, resource_delete,
        deployment_analyze, deployment_scale,
        statefulset_analyze, service_analyze, ingress_analyze,
        networkpolicy_analyze, cronjob_analyze,
        mutatingwebhook_analyze, validatingwebhook_analyze,
        workload_resource_usage,
    ]


if __name__ == "__main__":
    # 自测：python -m tools.mock_k8s
    from rich.console import Console

    console = Console()
    tools = load_mock_tools()
    console.print(f"[bold]mock 工具数:[/bold] {len(tools)}")
    console.print("[bold]工具名:[/bold] " + ", ".join(t.name for t in tools))
    console.print("\n[cyan]node_analyze 输出示例:[/cyan]")
    console.print(node_analyze.invoke({}))
    console.print("\n[cyan]pod_analyze 输出示例:[/cyan]")
    console.print(pod_analyze.invoke({"namespace": "kube-system"}))
