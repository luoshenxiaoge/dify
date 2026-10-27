# pgvector 双实例高可用切换方案

## 一、背景与目标

### 1.1 当前架构分析
Dify 当前使用单一 pgvector 实例作为向量数据库，配置结构如下：
- 配置类：`api/configs/middleware/vdb/pgvector_config.py` - `PGVectorConfig`
- 实现类：`api/core/rag/datasource/vdb/pgvector/pgvector.py` - `PGVector`
- 工厂类：`api/core/rag/datasource/vdb/pgvector/pgvector.py` - `PGVectorFactory`
- 配置加载：通过环境变量 `PGVECTOR_*` 加载单实例配置

### 1.2 需求目标
- 支持配置两个 pgvector 实例（主备模式）
- 主实例故障时自动切换到备用实例
- 切换过程对上层业务透明
- 支持手动切换和健康检查
- 数据一致性保证（通过外部 PostgreSQL 同步机制）

## 二、实现方案

### 2.1 整体架构设计

```
┌─────────────────────────────────────────────────────────┐
│                     上层应用 (Vector)                    │
└────────────────────────────┬────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────┐
│              PGVectorWithHA (高可用包装层)               │
│  ┌───────────────────────────────────────────────────┐  │
│  │  HealthChecker (健康检查器)                        │  │
│  └───────────────────────────────────────────────────┘  │
│  ┌───────────────────────────────────────────────────┐  │
│  │  FailoverManager (故障转移管理器)                  │  │
│  └───────────────────────────────────────────────────┘  │
└────────────────────┬────────────────────┬────────────────┘
                     │                    │
         ┌───────────┴──────────┐ ┌─────┴──────────┐
         ▼                      ▼ ▼                ▼
┌──────────────────┐  ┌──────────────────┐
│  PGVector (主)   │  │  PGVector (备)   │
│  实例 A          │  │  实例 B          │
└──────────────────┘  └──────────────────┘
```

### 2.2 核心模块设计

#### 2.2.1 配置模块扩展

**文件：** `api/configs/middleware/vdb/pgvector_config.py`

**变更内容：**
- 新增 `PGVectorInstanceConfig` 类表示单个 pgvector 实例配置
- 扩展 `PGVectorConfig` 支持主备两个实例配置
- 新增健康检查相关配置

```python
class PGVectorInstanceConfig(BaseModel):
    """单个 pgvector 实例配置"""
    host: str
    port: int
    user: str
    password: str
    database: str
    min_connection: int = 1
    max_connection: int = 5
    pg_bigm: bool = False

class PGVectorConfig(BaseSettings):
    """pgvector 高可用配置"""
    # 主实例配置 (兼容原有配置)
    PGVECTOR_HOST: str | None = None
    PGVECTOR_PORT: PositiveInt = 5433
    PGVECTOR_USER: str | None = None
    PGVECTOR_PASSWORD: str | None = None
    PGVECTOR_DATABASE: str | None = None
    PGVECTOR_MIN_CONNECTION: PositiveInt = 1
    PGVECTOR_MAX_CONNECTION: PositiveInt = 5
    PGVECTOR_PG_BIGM: bool = False
    
    # 备用实例配置 (新增)
    PGVECTOR_STANDBY_HOST: str | None = None
    PGVECTOR_STANDBY_PORT: PositiveInt = 5433
    PGVECTOR_STANDBY_USER: str | None = None
    PGVECTOR_STANDBY_PASSWORD: str | None = None
    PGVECTOR_STANDBY_DATABASE: str | None = None
    
    # 高可用配置 (新增)
    PGVECTOR_HA_ENABLED: bool = False  # 是否启用高可用
    PGVECTOR_HEALTH_CHECK_INTERVAL: PositiveInt = 10  # 健康检查间隔(秒)
    PGVECTOR_HEALTH_CHECK_TIMEOUT: PositiveInt = 5  # 健康检查超时(秒)
    PGVECTOR_FAILOVER_THRESHOLD: PositiveInt = 3  # 故障切换阈值(连续失败次数)
    
    def get_primary_config(self) -> PGVectorInstanceConfig:
        """获取主实例配置"""
        
    def get_standby_config(self) -> PGVectorInstanceConfig | None:
        """获取备用实例配置"""
```

#### 2.2.2 健康检查模块

**文件：** `api/core/rag/datasource/vdb/pgvector/health_checker.py` (新建)

**功能：**
- 定期检查 pgvector 实例健康状态
- 支持连接性检查和简单查询检查
- 记录健康状态历史

```python
class HealthStatus(Enum):
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"

class InstanceHealth:
    instance_id: str
    status: HealthStatus
    last_check_time: float
    consecutive_failures: int
    last_error: str | None

class HealthChecker:
    def __init__(self, instance_configs: dict[str, PGVectorInstanceConfig], 
                 check_interval: int = 10, 
                 check_timeout: int = 5):
        self.instance_configs = instance_configs
        self.check_interval = check_interval
        self.check_timeout = check_timeout
        self.health_states: dict[str, InstanceHealth] = {}
        self._running = False
        self._thread: threading.Thread | None = None
        
    def check_instance(self, instance_id: str) -> HealthStatus:
        """检查单个实例健康状态"""
        
    def start(self):
        """启动后台健康检查线程"""
        
    def stop(self):
        """停止健康检查"""
        
    def get_health_state(self, instance_id: str) -> InstanceHealth | None:
        """获取实例健康状态"""
```

#### 2.2.3 故障转移管理器

**文件：** `api/core/rag/datasource/vdb/pgvector/failover_manager.py` (新建)

**功能：**
- 监听健康检查结果
- 决策是否进行故障转移
- 执行实例切换
- 发布切换事件

```python
class FailoverEvent:
    timestamp: float
    from_instance: str
    to_instance: str
    reason: str

class FailoverManager:
    def __init__(self, health_checker: HealthChecker, 
                 failover_threshold: int = 3):
        self.health_checker = health_checker
        self.failover_threshold = failover_threshold
        self.current_instance: str = "primary"
        self._event_handlers: list[Callable[[FailoverEvent], None]] = []
        self._failover_history: list[FailoverEvent] = []
        
    def check_and_failover(self) -> bool:
        """检查并执行故障转移"""
        
    def switch_to(self, instance_id: str, reason: str):
        """手动切换到指定实例"""
        
    def on_failover(self, handler: Callable[[FailoverEvent], None]):
        """注册故障转移事件处理器"""
```

#### 2.2.4 高可用 PGVector 包装类

**文件：** `api/core/rag/datasource/vdb/pgvector/pgvector_ha.py` (新建)

**功能：**
- 封装主备两个 PGVector 实例
- 代理所有向量数据库操作到当前活跃实例
- 集成健康检查和故障转移
- 透明处理实例切换

```python
class PGVectorWithHA(BaseVector):
    """支持高可用的 pgvector 包装类"""
    
    def __init__(self, collection_name: str, config: PGVectorConfig):
        self.collection_name = collection_name
        self.config = config
        self._lock = threading.RLock()
        
        # 初始化主备实例
        self._primary: PGVector | None = None
        self._standby: PGVector | None = None
        self._init_instances()
        
        # 初始化健康检查和故障转移
        if config.PGVECTOR_HA_ENABLED:
            self._health_checker: HealthChecker | None = None
            self._failover_manager: FailoverManager | None = None
            self._init_ha()
        
    def _init_instances(self):
        """初始化主备 PGVector 实例"""
        
    def _init_ha(self):
        """初始化高可用组件"""
        
    @property
    def _active_instance(self) -> PGVector:
        """获取当前活跃实例"""
        
    # 实现 BaseVector 接口，代理到活跃实例
    def get_type(self) -> str:
        return VectorType.PGVECTOR
        
    def create(self, texts: list[Document], embeddings: list[list[float]], **kwargs):
        with self._lock:
            return self._retry_with_failover(lambda: self._active_instance.create(texts, embeddings, **kwargs))
            
    def add_texts(self, documents: list[Document], embeddings: list[list[float]], **kwargs):
        with self._lock:
            return self._retry_with_failover(lambda: self._active_instance.add_texts(documents, embeddings, **kwargs))
            
    # ... 其他 BaseVector 接口方法 ...
    
    def _retry_with_failover(self, func: Callable, max_retries: int = 2):
        """带故障转移的重试机制"""
        retries = 0
        last_error = None
        while retries < max_retries:
            try:
                return func()
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                last_error = e
                logger.warning(f"PGVector operation failed: {e}")
                if self._failover_manager and retries < max_retries - 1:
                    self._failover_manager.check_and_failover()
                retries += 1
        raise last_error
        
    def close(self):
        """关闭所有连接和后台线程"""
```

#### 2.2.5 工厂类修改

**文件：** `api/core/rag/datasource/vdb/pgvector/pgvector.py`

**变更内容：**
- 修改 `PGVectorFactory.init_vector()` 方法
- 根据配置决定返回普通 `PGVector` 还是 `PGVectorWithHA`

```python
class PGVectorFactory(AbstractVectorFactory):
    def init_vector(self, dataset: Dataset, attributes: list, embeddings: Embeddings) -> BaseVector:
        # ... 原有 collection_name 逻辑 ...
        
        if dify_config.PGVECTOR_HA_ENABLED and dify_config.PGVECTOR_STANDBY_HOST:
            # 使用高可用版本
            from .pgvector_ha import PGVectorWithHA
            return PGVectorWithHA(
                collection_name=collection_name,
                config=dify_config  # 传递完整配置
            )
        else:
            # 使用原有的单实例版本
            return PGVector(
                collection_name=collection_name,
                config=PGVectorConfig(
                    host=dify_config.PGVECTOR_HOST or "localhost",
                    port=dify_config.PGVECTOR_PORT,
                    user=dify_config.PGVECTOR_USER or "postgres",
                    password=dify_config.PGVECTOR_PASSWORD or "",
                    database=dify_config.PGVECTOR_DATABASE or "postgres",
                    min_connection=dify_config.PGVECTOR_MIN_CONNECTION,
                    max_connection=dify_config.PGVECTOR_MAX_CONNECTION,
                    pg_bigm=dify_config.PGVECTOR_PG_BIGM,
                ),
            )
```

## 三、配置示例

### 3.1 环境变量配置

```bash
# 主实例配置 (原有配置保持不变)
PGVECTOR_HOST=pg-primary.example.com
PGVECTOR_PORT=5433
PGVECTOR_USER=postgres
PGVECTOR_PASSWORD=your_password
PGVECTOR_DATABASE=postgres
PGVECTOR_MIN_CONNECTION=1
PGVECTOR_MAX_CONNECTION=5
PGVECTOR_PG_BIGM=false

# 备用实例配置 (新增)
PGVECTOR_STANDBY_HOST=pg-standby.example.com
PGVECTOR_STANDBY_PORT=5433
PGVECTOR_STANDBY_USER=postgres
PGVECTOR_STANDBY_PASSWORD=your_password
PGVECTOR_STANDBY_DATABASE=postgres

# 高可用开关配置 (新增)
PGVECTOR_HA_ENABLED=true
PGVECTOR_HEALTH_CHECK_INTERVAL=10
PGVECTOR_HEALTH_CHECK_TIMEOUT=5
PGVECTOR_FAILOVER_THRESHOLD=3
```

### 3.2 PostgreSQL 数据同步说明

**重要：** 本方案仅提供连接层的高可用切换，数据同步需要依赖 PostgreSQL 原生机制：

1. **流复制 (Streaming Replication)** - 推荐
   - 主实例开启 WAL 归档
   - 备实例作为热备副本
   - 支持同步或异步复制

2. **Patroni** - 自动化故障转移
   - 配合 etcd/consul 实现自动化主备切换
   - 支持自动故障检测和提升

3. **其他方案**
   - pgpool-II
   - repmgr

## 四、文件变更清单

### 4.1 新增文件
```
api/core/rag/datasource/vdb/pgvector/
├── health_checker.py       # 健康检查器
├── failover_manager.py     # 故障转移管理器
└── pgvector_ha.py          # 高可用 PGVector 包装类
```

### 4.2 修改文件
```
api/configs/middleware/vdb/
└── pgvector_config.py      # 扩展配置类

api/core/rag/datasource/vdb/pgvector/
└── pgvector.py             # 修改工厂类
```

## 五、实施步骤

### 阶段一：配置扩展
1. 修改 `pgvector_config.py`，添加双实例配置和 HA 配置
2. 更新配置文档和环境变量示例

### 阶段二：核心模块开发
1. 实现 `HealthChecker` 健康检查模块
2. 实现 `FailoverManager` 故障转移管理器
3. 实现 `PGVectorWithHA` 高可用包装类

### 阶段三：集成测试
1. 修改 `PGVectorFactory` 集成 HA 版本
2. 编写单元测试
3. 编写集成测试
4. 进行故障模拟测试

### 阶段四：文档和部署
1. 更新部署文档
2. 添加配置说明
3. 提供 PostgreSQL 同步配置指南

## 六、风险与注意事项

### 6.1 数据一致性
- 本方案不处理数据同步，需依赖 PostgreSQL 外部机制
- 建议使用同步复制模式以确保数据一致性

### 6.2 切换期间的请求处理
- 切换期间会有短暂的请求失败（自动重试）
- 建议在应用层实现幂等性

### 6.3 连接池管理
- 主备实例各自维护连接池
- 需要注意资源占用

### 6.4 监控与告警
- 建议添加健康状态监控
- 故障切换事件应触发告警

## 七、后续优化方向

1. **支持多备实例** - 不限于 1 主 1 备，支持 1 主多备
2. **读写分离** - 读请求走备实例，写请求走主实例
3. **连接池优化** - 实例切换后优雅关闭旧连接池
4. **Metrics 暴露** - 暴露健康检查、切换次数等 metrics
5. **配置热重载** - 支持不重启更新配置
