# OFD → PDF 转换器

本目录是 QASystem 的 OFD 文档转换组件，基于 [OFDRW](https://github.com/ofdrw/ofdrw) 将 OFD 文件转换为 PDF，供 `algorithm/preproc` 的文档解析链路使用。

## 构建

需要 JDK 17 + Maven 3.9+：

```bash
cd ofd_converter
mvn package
```

产物位于 `target/ofd-to-pdf-1.0.0.jar`。构建使用 `maven-shade-plugin` 打为单 jar，
已包含 `ofdrw-converter` 等依赖，主类为 `com.qasystem.ofd.OFDToPdf`。

## 本地使用

```bash
java -jar target/ofd-to-pdf-1.0.0.jar <input.ofd> <output.pdf>
```

不带参数运行会打印用法说明。

## 与系统集成的关系

- **构建阶段**：根目录 `Dockerfile` 会在此目录执行 `mvn package`，
  并把产物重命名为 `/opt/qasystem/ofdrw/ofd-to-pdf.jar` 拷入环境镜像。
- **运行时校验**：`docker/verify_runtime.py` 会检查该 jar 是否存在，
  并验证 `java -jar /opt/qasystem/ofdrw/ofd-to-pdf.jar` 能输出 `Usage: ofd-to-pdf`。
- **调用方**：`algorithm/preproc/preproc_src/processors/ofd_converter.py` 调用该 jar
  完成 OFD→PDF，随后 PDF 走常规文档解析流程。
