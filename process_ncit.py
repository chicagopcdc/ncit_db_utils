import sys
import pandas as pd
import zipfile
import psycopg
import argparse
import sqlalchemy
import os
import datetime
import requests
import tempfile
import time
import sqlite3

start_time = datetime.datetime.now()
evs_headers = {"Content-Type": "application/json"}
parser = argparse.ArgumentParser()
print(os.environ)
# These could be moved to environment variables.

# Expected arguments
parser.add_argument('--duckdb_file', action='store', type=str, required=False)
parser.add_argument('--dbname', action='store', type=str, required=False)
parser.add_argument('--host', action='store', type=str, required=False)
parser.add_argument('--port', action='store', type=int, required=False)
parser.add_argument('--user', action='store', type=str, required=False)
parser.add_argument('--schema', action='store', type=str, required=False)
parser.add_argument('--file',action='store', type=str, required=False, help="If a file name is specified, the script will write to a sqlite database")
args = parser.parse_args()

#fdconnection_string = f'postgresql+psycopg://{os.getenv('db_user')}:{os.getenv('db_password')}@{os.getenv('db_host')}:{os.getenv('db_port')}/{os.getenv('db_name')}'

#
# If the file name is specified, use sqlite3, otherwise, use Postgresql
# Need to add in Postgresql schema support
#

if args.file is not None:
    connection_string = f'sqlite:///{args.file}'
    print("connecting to sqlite database")
elif args.duckdb_file is not None:
    import duckdb
    connection_string = f'duckdb:///{args.duckdb_file}'
    print("Connecting to DuckDB")    
elif args.dbname is not None:
    print("connecting to Postgresql database")
    connection_string = f'postgresql+psycopg://{args.user}:{os.getenv('db_password')}@{args.host}:{args.port}/{args.dbname}'
else:
    print("no database connection info specified, bailing out.")
    sys.exit()
    
sae = sqlalchemy.create_engine(connection_string)
sae_connection = sae.connect()
sa_inspector = sqlalchemy.inspect(sae)
con = sae.raw_connection()
cur = con.cursor()

# get the list of table names in the db (used to see if this db has the version table or not).
tables_in_db = sa_inspector.get_table_names()

def get_concept_info(conceptlist : list, include: str, request_limit=500):
    """ return a set of concepts from EVS with their synonyms """

    url= f'https://api-evsrest.nci.nih.gov/api/v1/concept/ncit'
    retrieved_records = 0
    retry_count = 2
    sleep_time = 1
    backoff_increment = 3
    timeouts = 0


    res_list = []
    keep_going = True

    s = 0
    while s < len(conceptlist) and keep_going:
        chunk = conceptlist[s:s + request_limit]
        url_vars = {'include': include, 'list': chunk}
        try:
            r = requests.get(url, timeout=(0.4, 7.0), headers=evs_headers, params=url_vars)
            r.raise_for_status()

            j = r.json()
            res_list = res_list + j
            s = s + len(j)
        except requests.exceptions.HTTPError as http_err:
            print(f"HTTP error occurred: {http_err}")
            time.sleep(sleep_time + timeouts*backoff_increment)
            timeouts += 1
            if timeouts > retry_count:
                print("bailing out on concept info -- problems with EVS or networking or code")
                sys.exit()
        except Exception as err:
            print(f"Other error occurred: {err}")

            time.sleep(sleep_time+timeouts*backoff_increment)
            timeouts += 1
            if timeouts > retry_count:
                print("bailing out on concept info  -- problems with EVS or networking or code")
                sys.exit()


    return res_list


def get_inverse_associations_for_code(code: str):
    dataframe_data = {
        'association': pd.Series(dtype='str'),
        'code': pd.Series(dtype='str'),
        'name': pd.Series(dtype='str'),
        'relatedCode': pd.Series(dtype='str'),
        'relatedName': pd.Series(dtype='str')
    }
    association_df = pd.DataFrame(data=dataframe_data)
    retrieved_records = 0
    start_record = 0
    retry_count = 3
    sleep_time = 1


    # Data looks like :
    #[
    #    {
    #        "type": "Concept_In_Subset",
    #        "relatedCode": "C193958",
    #        "relatedName": "19q/19p Chromosome Deletion [Number Ratio] in Tissue by FISH"
    #    },

    page_size = 2500
    backoff_increment = 3
    print(f'Retrieving inverse association for code: {code}')
    #url = f'https://api-evsrest.nci.nih.gov/api/v1/concept/ncit/associations/{association_name}?fromRecord=0&pageSize=1'
    url =  f'https://api-evsrest.nci.nih.gov/api/v1/concept/ncit/{code}/inverseAssociations'

    r = requests.get(url,timeout=(0.4, 7.0), headers=evs_headers)
    if r.status_code != 200:
        return None

    j = r.json()

    new_df = pd.json_normalize(j)
    new_df = new_df.rename(columns={'type': 'association'})
    new_df['code'] = code
    concept_info = get_concept_info([code], 'minimal')
    new_df['name'] = concept_info[0]['name']
    new_df = new_df.reindex(['association', 'code', 'name', 'relatedCode', 'relatedName'], axis=1)
    association_df = pd.concat([association_df, new_df], ignore_index=True)

    association_df.columns = ['association', 'code', 'name', 'related_code', 'related_name']
    return association_df

def get_ncit_from_evs_api():
    """get the current evs from the api in case the flat file is not available"""
    pass

def divide_list(input_list, chunk_size):
    """Divide a list into smaller lists of a specified size."""
    return [input_list[i:i + chunk_size] for i in range(0, len(input_list), chunk_size)]

def get_full_synonyms_from_evs_api():
    """Get the full set of attributes for all synonyms from EVS."""

#    stuff = get_concept_info(['C194732'], 'synonyms')
    dataframe_data = {
        'code': pd.Series(dtype='str'),
        'name': pd.Series(dtype='str'),
        'source': pd.Series(dtype='str'),
        'subSource': pd.Series(dtype='str'),
        'termType': pd.Series(dtype='str'),
        'type': pd.Series(dtype='str')
    }
    full_synonym_df = pd.DataFrame(data=dataframe_data)
    cur = con.cursor()
    cur.execute("select code from ncit")
    rs = cur.fetchall()
    concept_list = [r[0] for r in rs]
    chunks = divide_list(concept_list, 500)
    chunk_count = 0
    for chunk in chunks:
        chunk_count += 1
        print("get full synonyms - processing chunk", chunk_count, "of", len(chunks))
        syns_from_evs = get_concept_info(chunk,'synonyms')
        for c in syns_from_evs:
            if 'synonyms' in c:
                syn_info = c['synonyms']
                new_df = pd.json_normalize(syn_info)
                new_df['code'] = c['code']
                full_synonym_df = pd.concat([full_synonym_df, new_df], ignore_index=True)

    full_synonym_df.to_sql('full_synonyms', con=sae_connection, if_exists='replace', index=False)
    print('.')


def get_named_association(association_name: str):
    dataframe_data = {
        'association': pd.Series(dtype='str'),
        'code': pd.Series(dtype='str'),
        'name': pd.Series(dtype='str'),
        'relatedCode': pd.Series(dtype='str'),
        'relatedName': pd.Series(dtype='str')
    }
    association_df = pd.DataFrame(data=dataframe_data)
    retrieved_records = 0
    start_record = 0
    retry_count = 3
    sleep_time = 1

    page_size = 2500
    backoff_increment = 3
    print(f'Retrieving named association: {association_name}')
    url = f'https://api-evsrest.nci.nih.gov/api/v1/concept/ncit/associations/{association_name}?fromRecord=0&pageSize=1'
    #inverse_by_code_url =  f'https://api-evsrest.nci.nih.gov/api/v1/concept/ncit/{code}/inverseAssociations'

    r = requests.get(url,timeout=(0.4, 7.0), headers=evs_headers)
    if r.status_code != 200:
        return None

    j = r.json()
    print(j)
    if 'total' in j:
        total_records = j['total']
    else:
        return None

    url = f'https://api-evsrest.nci.nih.gov/api/v1/concept/ncit/associations/{association_name}'

    keep_going = True
    timeouts = 0

    while keep_going:
        try:
            r = requests.get(url,timeout=(0.4, 7.0), params = {'fromRecord':start_record, 'pageSize':page_size}, headers=evs_headers)
            r.raise_for_status()
            print(r.url)
            j=r.json()
            records_in_batch = len(j['associationEntries'])
            print("number of records in batch:", records_in_batch, 'for start record:', start_record)
            if records_in_batch > 0:
                new_df = pd.json_normalize(j['associationEntries'])
                new_df = new_df.reindex(['association', 'code', 'name', 'relatedCode', 'relatedName'], axis=1)
                association_df = pd.concat([association_df, new_df], ignore_index=True)
            if records_in_batch == 0 or start_record + records_in_batch >= total_records:
                print("done")
                break

            start_record += len(j['associationEntries'])

        except requests.exceptions.HTTPError as http_err:
            print(f"HTTP error occurred: {http_err}")
            time.sleep(sleep_time+timeouts)
            timeouts += 1
            if timeouts > retry_count:
                print("bailing out on associations -- problems with EVS or networking or code")
                sys.exit()
        except Exception as err:
            print(f"Other error occurred: {err}")
            time.sleep(sleep_time+timeouts)
            timeouts += 1
            if timeouts > retry_count:
                print("bailing out on associations -- problems with EVS or networking or code")
                sys.exit()
    association_df.columns = ['association', 'code', 'name', 'related_code', 'related_name']
    return association_df






r = requests.get('https://api-evsrest.nci.nih.gov/api/v1/concept/ncit?include=minimal&list=C2991',
                 timeout = (0.4, 7.0), headers = evs_headers)
j  = r.json()
ncit_version = j[0]['version']
print("ncit_version", ncit_version)

if 'ncit_version' not in tables_in_db:
    if args.file is None:
        cur.execute("create table ncit_version(version varchar(20), process_date timestamp)")
    else:
        cur.execute("create table ncit_version(version varchar(20), process_date text)")

con.commit()

version_sql = sqlalchemy.sql.text("select count(*) from ncit_version where version = :version")
res = sae_connection.execute(version_sql, {"version": ncit_version})
is_current = res.fetchone()[0]
if is_current == 1:
    print("Database version of NCIt is current, exiting.")
    sys.exit()
else:
    print("processing new NCIt version", ncit_version)

#
# Otherwise we need to process the new ncit version
# See if the delimited file exists, if so use it.  Otherwise, do it the hard way via the EVS API (TBD)
#

print("processing ncit_version", ncit_version)
ncit_url = f'https://evs.nci.nih.gov/ftp1/NCI_Thesaurus/Thesaurus_{ncit_version}.FLAT.zip'

ncit_filename = f'Thesaurus_{ncit_version}.FLAT.zip'
print('ncit_url=', ncit_url)

f = tempfile.NamedTemporaryFile(suffix = ncit_filename)
with requests.get(ncit_url, stream=True,timeout = (0.4, 7.0), headers = evs_headers) as r:
    if r.status_code == requests.codes.ok:
        for chunk in r.iter_content(chunk_size=5000000):
            f.write(chunk)
    else:
        r.raise_for_status()


thesaurus_file_zip = f.name
arch = zipfile.ZipFile(thesaurus_file_zip, mode='r')
thesaurus_file = arch.open('Thesaurus.txt', mode='r')

ncit_df = pd.read_csv(thesaurus_file, delimiter = '\t', header = None,
                         names=('code', 'url', 'parents','synonyms',
                                'definition', 'display_name', 'concept_status', 'semantic_type', 'pref_name' )
                      )

ncit_df['pref_name'] = ncit_df.apply(
    lambda row: row['synonyms'].split('|')[0] , axis = 1
)

f.close()

print(ncit_df)

print('writing dataframe to db')
ncit_df.to_sql('ncit', con=sae_connection, if_exists='replace', index=False)
sae_connection.commit()

cur = con.cursor()
cur.execute("drop index if exists ncit_code_index")
cur.execute("create index ncit_code_index on ncit(code)")
con.commit()

# make a parents dataframe that has the code and parents field except the high top level codes that have no parents

print('creating parents dataframe)')
parents_df = ncit_df[['code', 'parents']].dropna(subset=['parents']).copy()

# Split the 'parents' column into separate rows

parents_expanded = parents_df['parents'].str.split('|', expand=True).stack().reset_index(level=1, drop=True)
parents_expanded.name = 'parent'

# Now merge that back in

parent_df = parents_df.join(parents_expanded).reset_index(drop=True)
parent_df['path'] = parent_df['parent'] + '|' + parent_df['code']
parent_df['level'] = 1

parent_df.drop(columns=['parents'], inplace=True)
parent_df.rename(columns={'code': 'concept'}, inplace=True)

print('writing parents dataframe to db')
parent_df.to_sql('parents', con=sae_connection, if_exists='replace', index=False)
sae_connection.commit()

cur.execute("drop index if exists par_concept_idx")
cur.execute("create index par_concept_idx on parents(concept)")
cur.execute("drop index if exists par_par_idx")
cur.execute("create index par_par_idx on parents(parent)")

con.commit()
#get_full_synonyms_from_evs_api()  This is really slow from the EVS API

syns_df = ncit_df[['code', 'synonyms']].dropna(subset=['synonyms']).copy()


syn_expanded = syns_df['synonyms'].str.split('|', expand=True).stack().reset_index(level=1, drop=True)
syn_expanded.name = 'synonym'
syn_df = syns_df.join(syn_expanded).reset_index(drop=True)


syn_df.drop(columns=['synonyms'], inplace=True)
print('writing synonym dataframe to db')
syn_df.to_sql('synonyms', con=sae_connection, if_exists='replace', index=False)
sae_connection.commit()
# Delete the pref names from the synonyms table

cur.execute("delete from synonyms  where exists (select ncit.code from ncit  where synonyms.code = ncit.code and ncit.pref_name =  synonyms.synonym)")
cur.execute("drop index if exists synonym_code_index")
cur.execute("create index synonym_code_index on synonyms(code)")
cur.execute("drop index if exists synonym_syn_index")
cur.execute("create index synonym_syn_index on synonyms(synonym)")
sae_connection.commit()


print('Creating enumerated path table')
cur.execute("drop table if exists ncit_tc_with_path")

#
# Recursive call that uses the parents table as the base step (path level 1)
# The enumerates all the paths from one node to another node, looking down the graph.
#

cur.execute('create table ncit_tc_with_path as with recursive ncit_tc_rows(parent, descendant, level, path ) as ' +
'(select parent, concept as descendant, level, path from parents union all '+
"select p.parent , n.descendant as descendant, n.level+1 as level ,  p.parent || '|' || n.path  as path " +
'from ncit_tc_rows n join parents p on n.parent = p.concept  ' +
') select * from ncit_tc_rows')


cur.execute('create index ncit_tc_path_parent on ncit_tc_with_path(parent)')
cur.execute('create index ncit_tc_path_descendant on ncit_tc_with_path(descendant)')
con.commit()

# Now create the transitive closure table.

print('creating transitive closure table')
cur.execute('drop table if exists ncit_tc')
cur.execute('create table ncit_tc(parent varchar(64), descendant varchar(64))')
con.commit()
print('inserting ncit_tc records')
cur.execute("insert into ncit_tc  select distinct parent, descendant from ncit_tc_with_path ")
cur.execute('create index ncit_tc_parent on ncit_tc (parent) ')
cur.execute('create index ncit_tc_descendant on ncit_tc (descendant) ')

con.commit()

# Put in the reflexive relations
# N.B. While technically not part of the transitive closure, these entries make sql-based subsumption operations
# work well.

print('adding in reflexive relations')
cur.execute(
'''with codes as 
(
select distinct parent as code from ncit_tc
union
select distinct descendant as code from ncit_tc
) 
insert into ncit_tc (parent, descendant) 
select c.code as parent, c.code as descendant from codes c
''')

cur.execute(
'''with codes as 
(
select distinct parent as code from ncit_tc
union
select distinct descendant as code from ncit_tc
) 
insert into ncit_tc_with_path (parent, descendant, level, path) 
select c.code, c.code, 0  , c.code  from codes c
''')

con.commit()

print('noting ncit version number and wrapping up')
cur.execute('delete from ncit_version')
con.commit()
update_version_sql = sqlalchemy.sql.text("insert into ncit_version (version, process_date) values (:version, :process_date)")
if args.file is None:
    parm_dict = {"version": ncit_version, "process_date": datetime.datetime.now()}
else:
    parm_dict = {"version": ncit_version, "process_date": datetime.datetime.now().isoformat()}
res = sae_connection.execute(update_version_sql, parm_dict)
sae_connection.commit()

# get inverse associations as needed

# Get the mCode set members (C193006)

inv_associations_df = get_inverse_associations_for_code('C193006')

print('....')
cur.execute("drop table if exists associations")
con.commit()
assoc_df = get_named_association('Has_Target')

assoc_df.to_sql('associations',  con=sae_connection, if_exists='append', index=False)
inv_associations_df.to_sql('associations',  con=sae_connection, if_exists='append', index=False)

#
# Now, create the mCODE linkage table for convenience
#

mcode_concept_list = inv_associations_df['related_code'].tolist()

mcode_targets = get_concept_info(mcode_concept_list, 'synonyms')

mcode_df_list = []
for m in mcode_targets:
   # print('procesing ', m)
    for s in m['synonyms']:
        if 'source' in s and s['source'] == 'mCode' and 'subSource' in s:
            row = [m['code'], m['name'], s['subSource'],s['code'], s['name'], s['termType'], s['type']]
            mcode_df_list.append(row)
            break

mcode_df = pd.DataFrame.from_records(mcode_df_list, columns=['code', 'name', 'subsource', 'target_code', 'target_name', 'term_type', 'type'])
mcode_df['ncit_code'] = 'ncit:'+mcode_df['code']
mcode_df['full_target_code'] = mcode_df['subsource'].str.replace(' ','').str.replace('-','') + ':' + mcode_df['target_code']

mcode_df.to_sql('mcode_links',  con=sae_connection, if_exists='replace', index=False)


end_time=datetime.datetime.now()
print("execute time is " + str(end_time - start_time))