"""Offline product declarations. Modes describe the exported DDL syntax."""

PRODUCTS = {
    'mysql': ('MySQL', ('mysql',)),
    'postgresql': ('PostgreSQL', ('postgres',)),
    'oracle': ('Oracle', ('oracle',)),
    'sqlserver': ('SQL Server', ('tsql',)),
    'oceanbase': ('OceanBase', ('mysql', 'oracle')),
    'goldendb': ('GoldenDB', ('mysql',)),
    'vastbase': ('海量数据库 / Vastbase', ('postgres',)),
    'dameng': ('达梦 / DM', ('oracle',)),
    'kingbase': ('人大金仓 / KingbaseES', ('postgres', 'oracle')),
    'opengauss': ('openGauss', ('postgres',)),
    'gaussdb': ('GaussDB', ('postgres', 'mysql', 'oracle')),
    'other': ('其他兼容数据库', ('mysql', 'postgres', 'oracle', 'tsql')),
}
DIALECTS = {'mysql':'MySQL', 'postgres':'PostgreSQL', 'oracle':'Oracle', 'tsql':'SQL Server'}


def resolve_product(product, mode, legacy_dialect):
    if not product:
        product = {'mysql':'mysql','postgres':'postgresql','oracle':'oracle','tsql':'sqlserver'}.get(legacy_dialect)
    if product not in PRODUCTS:
        raise ValueError('请选择支持的数据库产品')
    label, modes = PRODUCTS[product]
    dialect = mode or (modes[0] if len(modes) == 1 else None)
    if dialect not in modes:
        raise ValueError(f'请选择 {label} 导出文件对应的兼容模式')
    return product, dialect
