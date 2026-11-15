"""
MEET_RUDI platform — words a database identifier never is (decision #43, conventions §8).

No schema, table, column, domain, type, function, parameter, variable, trigger, policy, index or
constraint we create is named, in any letter case, after a word in these lists. A name that would
be one is made specific instead (`display_name`, not `name`; `version_number`, not `number`);
never quoted, misspelt or given a trailing underscore. packages/rudi_platform/tests/test_schema.py
holds every migration, and the runner's own bookkeeping table, to it.

The lists, as published on 2026-11-10:
  POSTGRES      every keyword of PostgreSQL 17 and 18, all four categories (reserved,
                unreserved, column name, type or function name): src/include/parser/kwlist.h.
                Our engine, so even its unreserved keywords are out.
  SQL_STANDARD  the reserved words of SQL-92, SQL:2016 and SQL:2023 (part 2, Foundation), from
                PostgreSQL's doc/src/sgml/keywords.
  ORACLE        Oracle Database 23ai SQL reserved words (SQL Language Reference, appendix D).
  MYSQL         the MySQL 8.4 keywords marked (R), reserved (Reference Manual 11.3).
  SQL_SERVER    the Transact-SQL reserved keywords (SQL Server and Azure SQL).

The other engines' reserved words are here because data leaves Postgres: exports, analytics and
BI tools read the same names, and a reserved word there needs quoting everywhere it is used.
Extend a list when an engine adds words; never remove one to make a name pass.
"""

from __future__ import annotations

# ruff: noqa: SIM905 -- word lists of hundreds of entries read better as text than as literals.

POSTGRES = frozenset(
    """
    abort absent absolute access action add admin after aggregate all also alter always analyse
    analyze and any array as asc asensitive assertion assignment asymmetric at atomic attach
    attribute authorization backward before begin between bigint binary bit boolean both breadth by
    cache call called cascade cascaded case cast catalog chain char character characteristics check
    checkpoint class close cluster coalesce collate collation column columns comment comments commit
    committed compression concurrently conditional configuration conflict connection constraint
    constraints content continue conversion copy cost create cross csv cube current current_catalog
    current_date current_role current_schema current_time current_timestamp current_user cursor
    cycle data database day deallocate dec decimal declare default defaults deferrable deferred
    definer delete delimiter delimiters depends depth desc detach dictionary disable discard
    distinct do document domain double drop each else empty enable encoding encrypted end enforced
    enum error escape event except exclude excluding exclusive execute exists explain expression
    extension external extract false family fetch filter finalize first float following for force
    foreign format forward freeze from full function functions generated global grant granted
    greatest group grouping groups handler having header hold hour identity if ilike immediate
    immutable implicit import in include including increment indent index indexes inherit inherits
    initially inline inner inout input insensitive insert instead int integer intersect interval
    into invoker is isnull isolation join json json_array json_arrayagg json_exists json_object
    json_objectagg json_query json_scalar json_serialize json_table json_value keep key keys label
    language large last lateral leading leakproof least left level like limit listen load local
    localtime localtimestamp location lock locked logged mapping match matched materialized maxvalue
    merge merge_action method minute minvalue mode month move name names national natural nchar
    nested new next nfc nfd nfkc nfkd no none normalize normalized not nothing notify notnull nowait
    null nullif nulls numeric object objects of off offset oids old omit on only operator option
    options or order ordinality others out outer over overlaps overlay overriding owned owner
    parallel parameter parser partial partition passing password path period placing plan plans
    policy position preceding precision prepare prepared preserve primary prior privileges
    procedural procedure procedures program publication quote quotes range read real reassign
    recheck recursive ref references referencing refresh reindex relative release rename repeatable
    replace replica reset restart restrict return returning returns revoke right role rollback
    rollup routine routines row rows rule savepoint scalar schema schemas scroll search second
    security select sequence sequences serializable server session session_user set setof sets share
    show similar simple skip smallint snapshot some source sql stable standalone start statement
    statistics stdin stdout storage stored strict string strip subscription substring support
    symmetric sysid system system_user table tables tablesample tablespace target temp template
    temporary text then ties time timestamp to trailing transaction transform treat trigger trim
    true truncate trusted type types uescape unbounded uncommitted unconditional unencrypted union
    unique unknown unlisten unlogged until update user using vacuum valid validate validator value
    values varchar variadic varying verbose version view views virtual volatile when where
    whitespace window with within without work wrapper write xml xmlattributes xmlconcat xmlelement
    xmlexists xmlforest xmlnamespaces xmlparse xmlpi xmlroot xmlserialize xmltable year yes zone
    """.split()
)

SQL_STANDARD = frozenset(
    """
    abs absent absolute acos action add all allocate alter and any any_value are array array_agg
    array_max_cardinality as asc asensitive asin assertion asymmetric at atan atomic authorization
    avg begin begin_frame begin_partition between bigint binary bit bit_length blob boolean both
    btrim by call called cardinality cascade cascaded case cast catalog ceil ceiling char
    char_length character character_length check classifier clob close coalesce collate collation
    collect column commit condition connect connection constraint constraints contains continue
    convert copy corr corresponding cos cosh count covar_pop covar_samp create cross cube cume_dist
    current current_catalog current_date current_default_transform_group current_path current_role
    current_row current_schema current_time current_timestamp current_transform_group_for_type
    current_user cursor cycle date day deallocate dec decfloat decimal declare default deferrable
    deferred define delete dense_rank deref desc describe descriptor deterministic diagnostics
    disconnect distinct domain double drop dynamic each element else empty end end-exec end_frame
    end_partition equals escape every except exception exec execute exists exp external extract
    false fetch filter first first_value float floor for foreign found frame_row free from full
    function fusion get global go goto grant greatest group grouping groups having hold hour
    identity immediate in indicator initial initially inner inout input insensitive insert int
    integer intersect intersection interval into is isolation join json json_array json_arrayagg
    json_exists json_object json_objectagg json_query json_scalar json_serialize json_table
    json_table_primitive json_value key lag language large last last_value lateral lead leading
    least left level like like_regex listagg ln local localtime localtimestamp log log10 lower lpad
    ltrim match match_number match_recognize matches max member merge method min minute mod modifies
    module month multiset names national natural nchar nclob new next no none normalize not
    nth_value ntile null nullif numeric occurrences_regex octet_length of offset old omit on one
    only open option or order out outer output over overlaps overlay pad parameter partial partition
    pattern per percent percent_rank percentile_cont percentile_disc period portion position
    position_regex power precedes precision prepare preserve primary prior privileges procedure ptf
    public range rank read reads real recursive ref references referencing regr_avgx regr_avgy
    regr_count regr_intercept regr_r2 regr_slope regr_sxx regr_sxy regr_syy relative release
    restrict result return returns revoke right rollback rollup row row_number rows rpad rtrim
    running savepoint schema scope scroll search second section seek select sensitive session
    session_user set show similar sin sinh size skip smallint some space specific specifictype sql
    sqlcode sqlerror sqlexception sqlstate sqlwarning sqrt start static stddev_pop stddev_samp
    submultiset subset substring substring_regex succeeds sum symmetric system system_time
    system_user table tablesample tan tanh temporary then time timestamp timezone_hour
    timezone_minute to trailing transaction translate translate_regex translation treat trigger trim
    trim_array true truncate uescape union unique unknown unnest update upper usage user using value
    value_of values var_pop var_samp varbinary varchar varying versioning view when whenever where
    width_bucket window with within without work write year zone
    """.split()
)

ORACLE = frozenset(
    """
    access add all alter and any as asc audit between by char check cluster column column_value
    comment compress connect create current date decimal default delete desc distinct drop else
    exclusive exists file float for from grant group having identified immediate in increment index
    initial insert integer intersect into is level like lock long maxextents minus mlslabel mode
    modify nested_table_id noaudit nocompress not nowait null number of offline on online option or
    order pctfree prior public raw rename resource revoke row rowid rownum rows select session set
    share size smallint start successful synonym sysdate table then to trigger uid union unique
    update user validate values varchar varchar2 view whenever where with
    """.split()
)

MYSQL = frozenset(
    """
    accessible add all alter analyze and as asc asensitive before between bigint binary blob both by
    call cascade case change char character check collate column condition constraint continue
    convert create cross cube cume_dist current_date current_time current_timestamp current_user
    cursor database databases day_hour day_microsecond day_minute day_second dec decimal declare
    default delayed delete dense_rank desc describe deterministic distinct distinctrow div double
    drop dual each else elseif empty enclosed escaped except exists exit explain false fetch
    first_value float float4 float8 for force foreign from fulltext function generated get grant
    group grouping groups having high_priority hour_microsecond hour_minute hour_second if ignore in
    index infile inner inout insensitive insert int int1 int2 int3 int4 int8 integer intersect
    interval into io_after_gtids io_before_gtids is iterate join json_table key keys kill lag
    last_value lateral lead leading leave left like limit linear lines load localtime localtimestamp
    lock long longblob longtext loop low_priority match maxvalue mediumblob mediumint mediumtext
    middleint minute_microsecond minute_second mod modifies natural no_write_to_binlog not nth_value
    ntile null numeric of on optimize optimizer_costs option optionally or order out outer outfile
    over partition percent_rank precision primary procedure purge qualify range rank read read_write
    reads real recursive references regexp release rename repeat replace require resignal restrict
    return revoke right rlike row row_number rows schema schemas second_microsecond select sensitive
    separator set show signal smallint spatial specific sql sql_big_result sql_calc_found_rows
    sql_small_result sqlexception sqlstate sqlwarning ssl starting stored straight_join system table
    tablesample terminated then tinyblob tinyint tinytext to trailing trigger true undo union unique
    unlock unsigned update usage use using utc_date utc_time utc_timestamp values varbinary varchar
    varcharacter varying virtual when where while window with write xor year_month zerofill
    """.split()
)

SQL_SERVER = frozenset(
    """
    add all alter and any as asc authorization backup begin between break browse bulk by cascade
    case check checkpoint close clustered coalesce collate column commit compute constraint contains
    containstable continue convert create cross current current_date current_time current_timestamp
    current_user cursor database dbcc deallocate declare default delete deny desc disk distinct
    distributed double drop dump else end errlvl escape except exec execute exists exit external
    fetch file fillfactor for foreign freetext freetexttable from full function goto grant group
    having holdlock identity identity_insert identitycol if in index inner insert intersect into is
    join key kill left like lineno load merge national nocheck nonclustered not null nullif of off
    offsets on open opendatasource openquery openrowset openxml option or order outer over percent
    pivot plan precision primary print proc procedure public raiserror read readtext reconfigure
    references replication restore restrict return revert revoke right rollback rowcount rowguidcol
    rule save schema securityaudit select semantickeyphrasetable semanticsimilaritydetailstable
    semanticsimilaritytable session_user set setuser shutdown some statistics system_user table
    tablesample textsize then to top tran transaction trigger truncate try_convert tsequal union
    unique unpivot update updatetext use user values varying view waitfor when where while with
    within writetext
    """.split()
)

LISTS: dict[str, frozenset[str]] = {
    "PostgreSQL": POSTGRES,
    "SQL standard": SQL_STANDARD,
    "Oracle": ORACLE,
    "MySQL": MYSQL,
    "SQL Server": SQL_SERVER,
}


def clashes(identifier: str) -> list[str]:
    """The lists `identifier` appears in (case-insensitively); empty when it is free to use."""
    word = identifier.lower()
    return [source for source, listed in LISTS.items() if word in listed]
