"""
Monkey patching pandas to add utilities for CARTO tables
Andy Eschbacher and Stuart Lynn, 2017

Project goals
    * Interact with a CARTO table fully within a Jupyter notebook/pandas
      workflow
    * Like geopandas, have a .cartomap() method which gives back the data
      as a map using carto's maps api and turbocartocss on an optional
      attribute
    * Add CARTO services like the Data Observatory as methods to a dataframe

Features to add:
    * create a dataframe from scratch
        * establish cartodb_id
        * set metadata manually
        * register with carto

Notes on propagating pandas metadata:
    * https://github.com/pandas-dev/pandas/issues/2485
    * geopandas does a good job of propagating metadata, seems to be by
      subclassing Dataframes:
      https://github.com/geopandas/geopandas/blob/v0.2.1/geopandas/geodataframe.py#L54
      similar to what we tried in cartopandas.py.
      A geodataframe stores it's own metadata:
      https://github.com/geopandas/geopandas/blob/v0.2.1/geopandas/geodataframe.py#L47
"""

# TODO: hook into pandas.core?
import pandas as pd
import cartodb

def add_meta(self, **kwargs):
    """
        Set metadata for a dataframe if none has been already set
    """
    for key in kwargs:
        self._metadata[0][key] = kwargs[key]

def map_dtypes(pgtype):
    """
        Map PostgreSQL data types to NumPy/pandas dtypes
    """
    dtypes = {'number': 'float64',
              'date': 'datetime64',
              'string': 'string',
              'geometry': 'string',
              'boolean': 'bool'}
    return dtypes[pgtype]

def transform_schema(pgschema):
    """
        Transform schema returned via SQL API to dict for pandas
    """
    datatypes = {}
    for field in pgschema:
        if 'cartodb_id' in field:
            continue
        datatypes[field] = map_dtypes(pgschema[field]['type'])
    return datatypes

def read_carto(self, cdb_client, username=None, tablename=None,
               custom_query=None, api_key=None, include_geom=True,
               limit=None, index='cartodb_id', debug=False):
    """Import a table from carto into a pandas dataframe, storing
       table information in pandas metadata"""
    # NOTE: need json or urllib anymore?
    # import json
    # import urllib

    # construct query
    if tablename:
        query = 'SELECT * FROM "{tablename}"'.format(tablename=tablename)
        if limit:
            # NOTE: what if limit is `all` or `none`?
            if (limit >= 0) and isinstance(limit, int):
                query += ' LIMIT {limit}'.format(limit=limit)
            else:
                raise ValueError("`limit` parameter must an integer >= 0")
    elif query:
        query = custom_query
    else:
        raise ValueError("`tablename` or `query` needs to be specified")

    if debug:
        print query

    # exclude geometry columns if asked
    # TODO: include_geom in cdb_client structure?
    if not include_geom:
        params = {}
        params['skipfields'] = 'the_geom,the_geom_webmercator'
    if debug:
        print query
    # TODO: use the dtype flag in read_csv to set the schema
    # TODO: use the na_values flag for turning nulls to NaNs deterministically
    # _df = pd.read_csv(api_endpoint + urllib.urlencode(params),
    #                  index_col='cartodb_id')
    resp = cdb_client.sql(query)
    schema = transform_schema(resp['fields'])
    _df = pd.DataFrame(resp['rows'], index=index).astype(schema)

    # TODO: add table schema to the metadata
    # NOTE: pylint complains that we're accessing a 'protected member
    #       _metadata of a client class' (appending to _metadata only works
    #       with strings, not JSON, so we're serializing here)
    _df._metadata[0] = json.dumps({'carto_table': tablename,
                                   'carto_username': username,
                                   'carto_api_key': api_key,
                                   'carto_include_geom': include_geom,
                                   'carto_limit': limit,
                                   'carto_schema': _df.columns})
    #_df.set_index('cartodb_id')
    self.carto_last_state = _df.copy(deep=True)
    return _df

pd.read_carto = read_carto


# TODO: add into update_carto function as subfunction?
def process_item(item):
    """
      map NumPy values to PostgreSQL values
    """
    from math import isnan
    if isinstance(item, str):
        return '\'{}\''.format(item)
    elif isinstance(item, float):
        if isnan(item):
            return 'null'
        return str(item)
    return str(item)

def datatype_map(dtype):
    """
       map NumPy types to PostgreSQL types
    """
    if 'float' in dtype:
        return 'numeric'
    elif 'int' in dtype:
        return 'int'
    else:
        return 'text'

# TODO: make less buggy about the diff between NaNs and nulls
# TODO: batch UPDATES into a transaction
# TODO: if table metadata doesn't exist, error saying need to set 'create'
#       flag
def update_carto(self, createtable=False, debug=False):
    import urllib
    import json
    import requests
    if createtable is True:
        # TODO: build this
        # grab df schema, setup table, cartodbfy, then exit
        pass
    elif len(self._metadata) == 0:
        raise Exception("Table not registered with CARTO. Set `createtable` "
                        "flag to True")

    api_endpoint = 'https://{username}.carto.com/api/v2/sql?'.format(
        username=json.loads(self._metadata[0])['carto_username'])

    if 'carto_api_key' in json.loads(self._metadata[0]):
        params = {
            'api_key': json.loads(self._metadata[0])['carto_api_key']
        }
    else:
        raise Exception("No API key set for this dataframe. Set with "
                        "update metadata method.")
    # update current state of dataframe
    # diff with the last retrieved version from carto
    # filename = 'carto_temp_{}'.format(
    #     json.loads(self._metadata[0])['carto_table'])
    # if debug: print filename
    # pd.read_csv(filename, index_col='cartodb_id')
    last_state = self.carto_last_state
    # if debug: print last_state.head()

    # create new column if needed
    # TODO: extract to function
    if len(set(self.columns) - set(last_state.columns)) > 0:
        newcols = set(self.columns) - set(last_state.columns)
        for col in newcols:
            if debug: print "Create new column {col}".format(col=col)
            alter_query = '''
                ALTER TABLE "{tablename}"
                ADD COLUMN "{colname}" {datatype}
            '''.format(tablename=json.loads(self._metadata[0])['carto_table'],
                       colname=col,
                       datatype=datatype_map(self.dtypes[col]))
            if debug: print alter_query
            params['q'] = alter_query
            # add column
            # TODO: replace with `carto-python` client
            resp = requests.get(api_endpoint + urllib.urlencode(params))
            if debug: print resp.text
            # update all the values in that column
            # NOTE: fails if colval is 'inf' or some other Python or NumPy type
            for item in self[col].iteritems():
                if debug: print item
                update_query = '''
                    UPDATE {tablename}
                    SET "{colname}" = {colval}
                    WHERE "cartodb_id" = {cartodb_id}
                '''.format(tablename=json.loads(self._metadata[0])['carto_table'],
                           colname=col,
                           colval=process_item(item[1]),
                           cartodb_id=item[0])
                if debug: print update_query
                params['q'] = update_query
                # TODO: replace with carto-python client
                resp = requests.get(api_endpoint + urllib.urlencode(params))
                if debug: print resp.text
    # drop column if needed
    # TODO: extract to function
    if len(set(last_state.columns) - set(self.columns)) > 0:
        discardedcols = set(last_state.columns) - set(self.columns)
        for col in discardedcols:
            alter_query = '''
                ALTER TABLE "{tablename}"
                DROP COLUMN "{colname}"
            '''.format(tablename=json.loads(self._metadata[0])['carto_table'],
                       colname=col)
            params['q'] = alter_query
            if debug: print alter_query
            # TODO: replace with carto-python client
            resp = requests.get(api_endpoint + urllib.urlencode(params))
            if debug: print resp.text
    # sync updated values
    # TODO: extract to column
    common_cols = list(set(self.columns) & set(last_state.columns))
    df_diff = (self[common_cols] != last_state[common_cols]).stack()
    for i in df_diff.iteritems():
        # TODO: instead of doing row by row, build up a list of queries
        #       testing to be sure the num of characters is lower than
        #       16368ish. And then run the query as a transaction
        if i[1]:
            if debug: print i
            cartodb_id = i[0][0]
            colname = i[0][1]
            upsert_query = '''
            INSERT INTO {tablename}("cartodb_id", "{colname}")
                 VALUES ({cartodb_id}, {colval})
            ON CONFLICT ("cartodb_id")
            DO UPDATE SET "{colname}" = {colval}
            WHERE EXCLUDED."cartodb_id" = {cartodb_id}
            '''.format(tablename=json.loads(self._metadata[0])['carto_table'],
                       colname=colname,
                       colval=process_item(self.loc[cartodb_id][colname]),
                       cartodb_id=cartodb_id)
            if debug: print upsert_query
            params['q'] = upsert_query
            # TODO: replace with carto-python client
            resp = requests.get(api_endpoint + urllib.urlencode(params))
            if debug: print json.loads(resp.text)
        else:
            continue

pd.DataFrame.update_carto = update_carto
