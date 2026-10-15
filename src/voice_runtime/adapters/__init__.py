"""真实模型适配器（可选依赖，extras: real）。

仅用于实时 demo：推理为同步调用，不进入核心测试，也不在虚拟 loop 中运行。
重依赖（onnxruntime / numpy / httpx / transformers）在构造函数内惰性导入，
因此未安装 extras 时 `import voice_runtime.adapters.*` 依然可用。

注意：各适配器的 I/O 形状、张量名与输出语义均为未经验证的占位，不保证能在真实模型/服务上运行。
"""
