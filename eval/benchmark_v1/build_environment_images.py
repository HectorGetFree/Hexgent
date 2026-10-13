"""只构建不可变 Benchmark 服务镜像；复制源码构建，不写 Production target。"""

import argparse
from hashlib import sha256
import io
from importlib.metadata import version
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import xml.etree.ElementTree as ET

from .environment import LocalDocker, dump


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace-source", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--docker-socket", required=True)
    parser.add_argument("--maven-cache", type=Path, required=True)
    args = parser.parse_args()
    args.root.mkdir(parents=True, exist_ok=False)
    runtime_source = Path(__file__).resolve().parents[2]
    docker = LocalDocker(args.docker_socket)
    metadata = {}
    for name, source in [("workspace", args.workspace_source), ("runtime", runtime_source)]:
        if subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], cwd=source).strip():
            raise RuntimeError("构建源有已追踪文件改动，拒绝使用未冻结版本")
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
        destination = args.root / name; destination.mkdir()
        archive = subprocess.check_output(["git", "archive", "HEAD"], cwd=source)
        with tarfile.open(fileobj=io.BytesIO(archive)) as data:
            data.extractall(destination, filter="data")
        metadata[name] = {"commit": commit, "source_archive_sha256": sha256(archive).hexdigest()}
    # Maven 默认插件缓存不完整；只给构建副本关闭测试生命周期，并固定已有插件。
    ns = "http://maven.apache.org/POM/4.0.0"
    ET.register_namespace('', ns)
    for pom in (args.root / 'workspace').rglob('pom.xml'):
        tree=ET.parse(pom); node=tree.getroot()
        build=node.find(f'{{{ns}}}build')
        if build is None: build=ET.SubElement(node, f'{{{ns}}}build')
        plugins=build.find(f'{{{ns}}}plugins')
        if plugins is None: plugins=ET.SubElement(build, f'{{{ns}}}plugins')
        plugin=ET.SubElement(plugins, f'{{{ns}}}plugin')
        ET.SubElement(plugin,f'{{{ns}}}artifactId').text='maven-surefire-plugin'
        executions=ET.SubElement(plugin,f'{{{ns}}}executions'); execution=ET.SubElement(executions,f'{{{ns}}}execution')
        ET.SubElement(execution,f'{{{ns}}}id').text='default-test'
        ET.SubElement(execution,f'{{{ns}}}phase').text='none'
        if pom.parent.name=='hex-workspace-api':
            for artifact, v in [('maven-resources-plugin','2.5'),('maven-compiler-plugin','3.0'),('maven-jar-plugin','3.3.0')]:
                plugin=ET.SubElement(plugins,f'{{{ns}}}plugin')
                ET.SubElement(plugin,f'{{{ns}}}artifactId').text=artifact
                ET.SubElement(plugin,f'{{{ns}}}version').text=v
        if pom.parent.name=='hex-workspace-app':
            for plugin in plugins.findall(f'{{{ns}}}plugin'):
                if plugin.findtext(f'{{{ns}}}artifactId')=='spring-boot-maven-plugin':
                    plugin.find(f'{{{ns}}}version').text='2.7.12'
        tree.write(pom,encoding='utf-8',xml_declaration=True)
    # Maven 与缓存离线只读挂载；只在复制的源码目录生成 target。
    docker.run(["run", "--rm", "--network", "none", "--volume", f"{args.root / 'workspace'}:/build",
        "--volume", f"{args.maven_cache}:/root/.m2/repository:ro", "--workdir", "/build",
        "maven:3.9-eclipse-temurin-17", "mvn", "-o", "-q", "-Dmaven.test.skip=true",
        "-Dmaven-surefire-plugin.version=2.22.2", "package"], timeout=300)
    metadata["workspace"]["build_overrides"] = {"maven.test.skip": True, "test_phase": "none",
        "api_build_plugins": "resources 2.5 / compiler 3.0 / jar 3.3.0; temporary POM only"}
    metadata['workspace']['build_overrides']['boot_packaging_plugin']='2.7.12; packaging only'
    app = args.root / "workspace-image"; app.mkdir()
    shutil.copyfile(args.root / "workspace/hex-workspace-app/target/hex-workspace-app.jar", app / "app.jar")
    (app / "Dockerfile").write_text('FROM eclipse-temurin:17-jre-jammy\nCOPY app.jar /app.jar\nENTRYPOINT ["java","-jar","/app.jar"]\n')
    workspace_tag = "hexgent-b1b-workspace:" + metadata["workspace"]["commit"][:12]
    docker.run(["build", "--tag", workspace_tag, str(app)], timeout=300)
    # 使用已存在的 Python 基础镜像；安装软件依赖，不调用任何模型服务。
    runtime = args.root / "runtime"
    dependencies = " ".join(f"{name}=={version(name)}" for name in
                            ["fastapi", "uvicorn", "openai", "python-dotenv", "httpx", "pydantic"])
    metadata["runtime"]["dependencies"] = dependencies.split()
    (runtime / "Dockerfile.benchmark").write_text('''FROM docker.xuanyuan.run/library/python:3.12-slim
WORKDIR /opt/hexgent
COPY pyproject.toml ./
RUN pip install --no-cache-dir ''' + dependencies + '''
COPY hexgent ./hexgent
ENV PYTHONPATH=/opt/hexgent
''')
    # 明确排除凭证、Runtime 状态与评测产物，不复制整个仓库进镜像。
    (runtime / ".dockerignore").write_text('*\n!pyproject.toml\n!hexgent/\n!hexgent/**\n!Dockerfile.benchmark\n')
    runtime_tag = "hexgent-b1b-runtime:" + metadata["runtime"]["commit"][:12]
    docker.run(["build", "--file", str(runtime / "Dockerfile.benchmark"), "--tag", runtime_tag, str(runtime)], timeout=300)
    for name, tag in [("workspace", workspace_tag), ("runtime", runtime_tag)]:
        metadata[name]["image"] = tag
        metadata[name]["image_id"] = json.loads(docker.run(["image", "inspect", tag]))[0]["Id"]
    dump(args.root / "build-metadata.json", metadata)
    print(json.dumps(metadata))


if __name__ == "__main__":
    main()
