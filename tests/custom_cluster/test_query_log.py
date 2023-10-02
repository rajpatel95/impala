# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

from __future__ import absolute_import, division, print_function

import os
import pytest
import string
import tempfile

from random import choice, randint
from signal import SIGRTMIN
from tests.common.custom_cluster_test_suite import CustomClusterTestSuite
from tests.common.test_vector import ImpalaTestDimension
from tests.util.retry import retry
from tests.util.workload_management import assert_query, COMPRESSED_BYTES_SPILLED, \
    BYTES_READ_CACHE_TOTAL
from time import sleep, time


class TestQueryLogTableBase(CustomClusterTestSuite):
  """Base class for all query log tests. Sets up the tests to use the Beeswax and HS2
     client protocols."""

  WM_DB = "sys"
  QUERY_TBL = "{0}.impala_query_log".format(WM_DB)

  @classmethod
  def add_test_dimensions(cls):
    super(TestQueryLogTableBase, cls).add_test_dimensions()
    cls.ImpalaTestMatrix.add_dimension(ImpalaTestDimension('protocol', 'beeswax', 'hs2'))


class TestQueryLogTableBeeswax(TestQueryLogTableBase):
  """Tests to assert the query log table is correctly populated when using the Beeswax
     client protocol."""

  @classmethod
  def add_test_dimensions(cls):
    super(TestQueryLogTableBeeswax, cls).add_test_dimensions()
    cls.ImpalaTestMatrix.add_constraint(lambda v:
        v.get_value('protocol') == 'beeswax')

  CACHE_DIR = tempfile.mkdtemp(prefix="cache_dir")
  MAX_SQL_PLAN_LEN = 2000
  LOG_DIR_MAX_WRITES = tempfile.mkdtemp(prefix="max_writes")
  FLUSH_MAX_RECORDS_CLUSTER_ID = "test_query_log_max_records_" + str(int(time()))
  FLUSH_MAX_RECORDS_QUERY_COUNT = 2
  OTHER_TBL = "completed_queries_table_{0}".format(int(time()))

  @CustomClusterTestSuite.with_args(impalad_args="--enable_workload_mgmt "
                                                 "--query_log_write_interval_s=1 "
                                                 "--cluster_id=test_max_select "
                                                 "--shutdown_grace_period_s=10 "
                                                 "--shutdown_deadline_s=60 "
                                                 "--query_log_max_sql_length={0} "
                                                 "--query_log_max_plan_length={0}"
                                                 .format(MAX_SQL_PLAN_LEN),
                                    catalogd_args="--enable_workload_mgmt",
                                    impalad_graceful_shutdown=True)
  def test_query_log_table_lower_max_sql_plan(self, vector):
    """Asserts that lower limits on the sql and plan columns in the completed queries
       table are respected."""
    client = self.create_impala_client(protocol=vector.get_value('protocol'))
    rand_long_str = "".join(choice(string.ascii_letters) for _ in
        range(self.MAX_SQL_PLAN_LEN))

    try:
      handle = client.execute_async("select '{0}'".format(rand_long_str))
      query_id = handle.get_handle().id
      client.wait_for_finished_timeout(handle, 10)
      client.close_query(handle)

      self.cluster.get_first_impalad().service.wait_for_metric_value(
          "impala-server.completed-queries.written", 1, 60)

      # Force Impala to process the inserts to the completed queries table.
      client.execute("refresh " + self.QUERY_TBL)

      res = client.execute("select length(sql),plan from {0} where query_id='{1}'"
          .format(self.QUERY_TBL, query_id))
      assert res.success
      assert len(res.data) == 1

      data = res.data[0].split("\t")
      assert len(data) == 2
      assert int(data[0]) == self.MAX_SQL_PLAN_LEN - 1, "incorrect sql statement length"
      assert len(data[1]) == self.MAX_SQL_PLAN_LEN - data[1].count("\n") - 1, \
          "incorrect plan length"

    finally:
      client.close()

  @CustomClusterTestSuite.with_args(impalad_args="--enable_workload_mgmt "
                                                 "--query_log_write_interval_s=1 "
                                                 "--cluster_id=test_max_select "
                                                 "--shutdown_grace_period_s=10 "
                                                 "--shutdown_deadline_s=60",
                                    catalogd_args="--enable_workload_mgmt",
                                    impalad_graceful_shutdown=True)
  def test_query_log_table_over_max_sql_plan(self, vector):
    """Asserts that very long queries have their corresponding plan and sql columns
       shortened in the completed queries table."""
    client = self.create_impala_client(protocol=vector.get_value('protocol'))
    rand_long_str = "".join(choice(string.ascii_letters) for _ in range(16778200))

    try:
      client.set_configuration_option("MAX_STATEMENT_LENGTH_BYTES", 16780000)
      handle = client.execute_async("select '{0}'".format(rand_long_str))
      query_id = handle.get_handle().id
      client.wait_for_finished_timeout(handle, 10)
      client.close_query(handle)

      self.cluster.get_first_impalad().service.wait_for_metric_value(
          "impala-server.completed-queries.written", 1, 60)

      # Force Impala to process the inserts to the completed queries table.
      client.execute("refresh " + self.QUERY_TBL)

      client.set_configuration_option("MAX_ROW_SIZE", 35000000)
      res = client.execute("select length(sql),plan from {0} where query_id='{1}'"
          .format(self.QUERY_TBL, query_id))
      assert res.success
      assert len(res.data) == 1
      data = res.data[0].split("\t")
      assert len(data) == 2
      assert data[0] == "16777215"

      # Newline characters are not counted by Impala's length function.
      assert len(data[1]) == 16777216 - data[1].count("\n") - 1

    finally:
      client.close()

  @CustomClusterTestSuite.with_args(impalad_args="--enable_workload_mgmt "
                                                 "--query_log_write_interval_s=1 "
                                                 "--cluster_id=test_query_hist_1 "
                                                 "--shutdown_grace_period_s=10 "
                                                 "--shutdown_deadline_s=60 "
                                                 "--query_log_size=0 "
                                                 "--query_log_size_in_bytes=0",
                                    catalogd_args="--enable_workload_mgmt",
                                    impalad_graceful_shutdown=True)
  def test_query_log_table_no_query_log_select(self, vector):
    """Asserts queries are written to the completed queries table when the query log is
       turned off."""
    client = self.create_impala_client(protocol=vector.get_value('protocol'))

    try:
      # Run a select query.
      random_val = randint(1, 1000000)
      select_sql = "select {0}".format(random_val)
      res = client.execute(select_sql)
      assert res.success
      self.cluster.get_first_impalad().service.wait_for_metric_value(
          "impala-server.completed-queries.written", 1, 60)

      # Force Impala to process the inserts to the completed queries table.
      client.execute("refresh " + self.QUERY_TBL)

      actual = client.execute("select sql from {0} where query_id='{1}'".format(
          self.QUERY_TBL, res.query_id))
      assert actual.success
      assert len(actual.data) == 1
      assert actual.data[0] == select_sql
    finally:
      client.close()

  @CustomClusterTestSuite.with_args(impalad_args="--enable_workload_mgmt "
                                                 "--query_log_write_interval_s=1 "
                                                 "--cluster_id=test_query_hist_2 "
                                                 "--shutdown_grace_period_s=10 "
                                                 "--shutdown_deadline_s=60 "
                                                 "--always_use_data_cache "
                                                 "--data_cache={0}:5GB".format(CACHE_DIR),
                                    catalogd_args="--enable_workload_mgmt",
                                    impalad_graceful_shutdown=True,
                                    cluster_size=1)
  def test_query_log_table_query_cache(self, vector):
    """Asserts the values written to the query log table match the values from the
       query profile. Specifically focuses on the data cache metrics."""
    tbl_name = "default.test_query_log_cache" + str(int(time()))
    client = self.create_impala_client(protocol=vector.get_value('protocol'))

    try:
      # Create the test table.
      create_tbl_sql = "create table {0} (id INT, product_name STRING) " \
        "partitioned by (category INT)".format(tbl_name)
      create_tbl_results = client.execute(create_tbl_sql)
      assert create_tbl_results.success

      # Insert some rows into the test table.
      insert_sql = "insert into {0} (id,category,product_name) VALUES ".format(tbl_name)
      for i in range(1, 11):
        for j in range(1, 11):
          if i * j > 1:
            insert_sql += ","

          random_product_name = "".join(choice(string.ascii_letters)
            for _ in range(10))
          insert_sql += "({0},{1},'{2}')".format((i * j), i, random_product_name)

      insert_results = client.execute(insert_sql)
      assert insert_results.success

      # Select all rows from the test table. Run the query multiple times to ensure data
      # is cached.
      select_sql = "select * from {0}".format(tbl_name)
      for i in range(3):
        res = client.execute(select_sql)
        assert res.success
        sleep(1)

      self.cluster.get_first_impalad().service.wait_for_metric_value(
          "impala-server.completed-queries.written", 5, 60)

      # Allow some time for the cache to be written to disk.
      sleep(10)

      # Run the same query again so results are read from the data cache.
      res = client.execute(select_sql, fetch_profile_after_close=True)
      assert res.success
      self.cluster.get_first_impalad().service.wait_for_metric_value(
          "impala-server.completed-queries.written", 6, 60)

      data = assert_query(self.QUERY_TBL, client, "test_query_hist_2",
          res.runtime_profile)

      # Since the assert_query function only asserts that the bytes read from cache
      # column is equal to the bytes read from cache in the profile, there is a potential
      # for this test to not actually assert anything different than other tests. Thus, an
      # additional assert is needed to ensure that there actually was data read from the
      # cache.
      assert data[BYTES_READ_CACHE_TOTAL] != "0", "bytes read from cache total was " \
          "zero, test did not assert anything"
    finally:
      client.execute("drop table if exists {0}".format(tbl_name))
      client.close()

  @CustomClusterTestSuite.with_args(impalad_args="--enable_workload_mgmt "
                                                 "--query_log_write_interval_s=5 "
                                                 "--shutdown_grace_period_s=10 "
                                                 "--shutdown_deadline_s=60 "
                                                 "--log_dir={0}"
                                                 .format(LOG_DIR_MAX_WRITES),
                                    catalogd_args="--enable_workload_mgmt",
                                    impalad_graceful_shutdown=True)
  def test_query_log_max_attempts_exceeded(self, vector):
    """Asserts that completed queries are only attempted 3 times to be inserted into the
       completed queries table. This test deletes the completed queries table thus it must
       not come last otherwise the table stays deleted. Subsequent tests will re-create
       the table."""

    print("USING LOG DIRECTORY: {0}".format(self.LOG_DIR_MAX_WRITES))

    impalad = self.cluster.get_first_impalad()
    client = self.create_impala_client(protocol=vector.get_value('protocol'))

    try:
      res = client.execute("drop table {0} purge".format(self.QUERY_TBL))
      assert res.success
      impalad.service.wait_for_metric_value(
          "impala-server.completed-queries.scheduled-writes", 3, 60)
      impalad.service.wait_for_metric_value("impala-server.completed-queries.failure", 3,
          60)

      query_count = 0

      # Allow time for logs to be written to disk.
      sleep(5)

      with open(os.path.join(self.LOG_DIR_MAX_WRITES, "impalad.ERROR")) as file:
        for line in file:
          if line.find('could not write completed query table="{0}" query_id="{1}"'
                           .format(self.QUERY_TBL, res.query_id)) >= 0:
            query_count += 1

      assert query_count == 1

      assert impalad.service.get_metric_value(
        "impala-server.completed-queries.max-records-writes") == 0
      assert impalad.service.get_metric_value(
        "impala-server.completed-queries.queued") == 0
      assert impalad.service.get_metric_value(
        "impala-server.completed-queries.failure") == 3
      assert impalad.service.get_metric_value(
        "impala-server.completed-queries.scheduled-writes") == 4
      assert impalad.service.get_metric_value(
        "impala-server.completed-queries.written") == 0
    finally:
      client.close()

  @CustomClusterTestSuite.with_args(impalad_args="--enable_workload_mgmt "
                                                 "--query_log_max_queued={0} "
                                                 "--query_log_write_interval_s=9999 "
                                                 "--shutdown_grace_period_s=10 "
                                                 "--shutdown_deadline_s=60 "
                                                 "--cluster_id={1}"
                                                 .format(FLUSH_MAX_RECORDS_QUERY_COUNT,
                                                 FLUSH_MAX_RECORDS_CLUSTER_ID),
                                    catalogd_args="--enable_workload_mgmt",
                                    impalad_graceful_shutdown=True)
  def test_query_log_flush_max_records(self, vector):
    """Asserts that queries that have completed are written to the query log table when
       the maximum number of queued records it reached."""

    impalad = self.cluster.get_first_impalad()
    client = self.create_impala_client(protocol=vector.get_value('protocol'))

    rand_str = "{0}-{1}".format(vector.get_value('protocol'), time())

    test_sql = "select '{0}','{1}'".format(rand_str,
        self.FLUSH_MAX_RECORDS_CLUSTER_ID)
    test_sql_assert = "select '{0}', count(*) from {1} where sql='{2}'".format(
        rand_str, self.QUERY_TBL, test_sql.replace("'", r"\'"))

    try:
      for _ in range(0, self.FLUSH_MAX_RECORDS_QUERY_COUNT):
        res = client.execute(test_sql)
        assert res.success

      # Running this query results in the number of queued completed queries to exceed
      # the max and thus all completed queries will be written to the query log table.
      res = client.execute(test_sql_assert)
      assert res.success
      assert 1 == len(res.data)
      assert "0" == res.data[0].split("\t")[1]

      # Wait until the completed queries have all been written out because the max queued
      # count was exceeded.
      impalad.service.wait_for_metric_value(
          "impala-server.completed-queries.max-records-writes", 1, 60)

      # Force Impala to process the inserts to the completed queries table.
      sleep(5)
      client.execute("refresh " + self.QUERY_TBL)

      # This query will remain queued due to the long write interval and max queued
      # records limit not being reached.
      res = client.execute(r"select count(*) from {0} where sql like 'select \'{1}\'%'"
          .format(self.QUERY_TBL, rand_str))
      assert res.success
      assert 1 == len(res.data)
      assert "3" == res.data[0]
      impalad.service.wait_for_metric_value(
          "impala-server.completed-queries.queued", 2, 60)
    finally:
      client.close()

    assert impalad.service.get_metric_value(
        "impala-server.completed-queries.max-records-writes") == 1
    assert impalad.service.get_metric_value(
        "impala-server.completed-queries.scheduled-writes") == 0
    assert impalad.service.get_metric_value("impala-server.completed-queries.written") \
        == self.FLUSH_MAX_RECORDS_QUERY_COUNT + 1
    assert impalad.service.get_metric_value(
        "impala-server.completed-queries.queued") == 2

  @CustomClusterTestSuite.with_args(impalad_args="--enable_workload_mgmt "
                                                 "--query_log_write_interval_s=1 "
                                                 "--shutdown_grace_period_s=10 "
                                                 "--shutdown_deadline_s=30 "
                                                 "--blacklisted_dbs=information_schema "
                                                 "--query_log_table_name={0}"
                                                 .format(OTHER_TBL),
                                    catalogd_args="--enable_workload_mgmt "
                                                  "--blacklisted_dbs=information_schema",
                                    impalad_graceful_shutdown=True)
  def test_query_log_table_different_table(self, vector):
    """Asserts that queries that have completed but are not yet written to the query
       log table are flushed to the table before the coordinator exits."""

    client = self.create_impala_client(protocol=vector.get_value('protocol'))

    try:
      res = client.execute("show tables in {0}".format(self.WM_DB))
      assert res.success
      assert len(res.data) > 0, "could not find any tables in database {0}" \
          .format(self.DB)

      tbl_found = False
      for tbl in res.data:
        if tbl.startswith(self.OTHER_TBL):
          tbl_found = True
          break
      assert tbl_found, "could not find table '{0}' in database '{1}'" \
          .format(self.OTHER_TBL, self.DB)
    finally:
      client.execute("drop table {0}.{1} purge".format(self.WM_DB, self.OTHER_TBL))
      client.close()


class TestQueryLogTableHS2(TestQueryLogTableBase):
  """Tests to assert the query log table is correctly populated when using the HS2
     client protocol."""

  @classmethod
  def add_test_dimensions(cls):
    super(TestQueryLogTableHS2, cls).add_test_dimensions()
    cls.ImpalaTestMatrix.add_constraint(lambda v:
        v.get_value('protocol') == 'hs2')

  @CustomClusterTestSuite.with_args(impalad_args="--enable_workload_mgmt "
                                                 "--query_log_write_interval_s=1 "
                                                 "--cluster_id=test_query_hist_mult "
                                                 "--shutdown_grace_period_s=10 "
                                                 "--shutdown_deadline_s=60",
                                    catalogd_args="--enable_workload_mgmt",
                                    cluster_size=2,
                                    impalad_graceful_shutdown=True)
  def test_query_log_table_query_multiple(self, vector):
    """Asserts the values written to the query log table match the values from the
       query profile for a query that reads from multiple tables."""
    tbl_name = "default.test_query_log_" + str(int(time()))
    client = self.create_impala_client(protocol=vector.get_value('protocol'))

    try:
      # Create the first test table.
      create_tbl_sql = "create table {0}_products (id INT, product_name STRING)" \
          .format(tbl_name)
      create_tbl_results = client.execute(create_tbl_sql)
      assert create_tbl_results.success

      # Insert some rows into the test products table.
      insert_sql = "insert into {0}_products (id,product_name) VALUES ".format(tbl_name)
      for i in range(1, 11):
        for j in range(1, 11):
          if i * j > 1:
            insert_sql += ","

          random_product_name = "".join(choice(string.ascii_letters) for _ in range(10))
          insert_sql += "({0},'{1}')".format((i * j), random_product_name)

      insert_results = client.execute(insert_sql)
      assert insert_results.success

      # Create the second test table.
      create_tbl_sql = "create table {0}_customers (id INT, name STRING) " \
          .format(tbl_name)
      create_tbl_results = client.execute(create_tbl_sql)
      assert create_tbl_results.success

      # Insert rows into the test customers table.
      insert_sql = "insert into {0}_customers (id,name) VALUES ".format(tbl_name)
      for i in range(1, 11):
        if i > 1:
          insert_sql += ","
        rand_cust_name = "".join(choice(string.ascii_letters) for _ in range(10))
        insert_sql += "({0},'{1}')".format(i, rand_cust_name)

      insert_results = client.execute(insert_sql)
      assert insert_results.success

      # Create the third test table.
      create_tbl_sql = "create table {0}_sales (id INT, product_id INT, " \
          "customer_id INT) ".format(tbl_name)
      create_tbl_results = client.execute(create_tbl_sql)
      assert create_tbl_results.success

      # Insert rows into the test sales table.
      insert_sql = "insert into {0}_sales (id, product_id, customer_id) VALUES " \
          .format(tbl_name)
      for i in range(1, 1001):
        if i != 1:
          insert_sql += ","
        insert_sql += "({0},{1},{2})".format(i * j, randint(1, 100), randint(1, 10))

      insert_results = client.execute(insert_sql)
      assert insert_results.success

      # Select all rows from the test table.
      client.set_configuration_option("MAX_MEM_ESTIMATE_FOR_ADMISSION", "10MB")
      res = client.execute("select s.id, p.product_name, c.name from {0}_sales s "
          "inner join {0}_products p on s.product_id=p.id "
          "inner join {0}_customers c on s.customer_id=c.id".format(tbl_name),
          fetch_profile_after_close=True)
      assert res.success
      self.cluster.get_first_impalad().service.wait_for_metric_value(
          "impala-server.completed-queries.written", 7, 60)

      client2 = self.create_client_for_nth_impalad(1, vector.get_value('protocol'))
      assert client2 is not None
      assert_query(self.QUERY_TBL, client2, "test_query_hist_mult", res.runtime_profile,
          max_mem_for_admission=10485760)
    finally:
      client.execute("drop table if exists {0}_sales".format(tbl_name))
      client.execute("drop table if exists {0}_customers".format(tbl_name))
      client.execute("drop table if exists {0}_products".format(tbl_name))
      client.close()

  @CustomClusterTestSuite.with_args(impalad_args="--enable_workload_mgmt "
                                                 "--query_log_write_interval_s=1 "
                                                 "--cluster_id=test_query_hist_3 "
                                                 "--shutdown_grace_period_s=10 "
                                                 "--shutdown_deadline_s=60",
                                    catalogd_args="--enable_workload_mgmt",
                                    impalad_graceful_shutdown=True)
  def test_query_log_table_query_insert_select(self, vector):
    """Asserts the values written to the query log table match the values from the
       query profile for a query that insert selects."""
    tbl_name = "default.test_query_log_insert_select" + str(int(time()))
    client = self.create_impala_client(protocol=vector.get_value('protocol'))

    try:
      # Create the source test table.
      assert client.execute("create table {0}_source (id INT, product_name STRING) "
          .format(tbl_name)).success, "could not create source table"

      # Insert some rows into the test table.
      insert_sql = "insert into {0}_source (id,product_name) VALUES " \
          .format(tbl_name)
      for i in range(1, 100):
        if i > 1:
          insert_sql += ","

        random_product_name = "".join(choice(string.ascii_letters)
          for _ in range(10))
        insert_sql += "({0},'{1}')".format(i, random_product_name)

      assert client.execute(insert_sql).success, "could not insert rows"

      # Create the destination test table.
      assert client.execute("create table {0}_dest (id INT, product_name STRING) "
          .format(tbl_name)).success, "could not create destination table"

      # Insert select from the source table to the destination table.
      res = client.execute("insert into {0}_dest (id, product_name) select id, "
          "product_name from {0}_source".format(tbl_name), fetch_profile_after_close=True)
      assert res.success, "could not insert select"

      self.cluster.get_first_impalad().service.wait_for_metric_value(
          "impala-server.completed-queries.written", 4, 60)

      client2 = self.create_client_for_nth_impalad(2, vector.get_value('protocol'))
      assert client2 is not None
      assert_query(self.QUERY_TBL, client2, "test_query_hist_3", res.runtime_profile)
    finally:
      client.execute("drop table if exists {0}_source".format(tbl_name))
      client.execute("drop table if exists {0}_dest".format(tbl_name))
      client.close()

  @CustomClusterTestSuite.with_args(impalad_args="--enable_workload_mgmt "
                                                 "--query_log_write_interval_s=15 "
                                                 "--shutdown_grace_period_s=10 "
                                                 "--shutdown_deadline_s=60",
                                    catalogd_args="--enable_workload_mgmt",
                                    impalad_graceful_shutdown=True)
  def test_query_log_table_flush_interval(self, vector):
    """Asserts that queries that have completed are written to the query log table
       after the specified write interval elapses."""

    client = self.create_impala_client(protocol=vector.get_value('protocol'))

    try:
      query_count = 10

      for i in range(query_count):
        res = client.execute("select sleep(1000)")
        assert res.success

      # At least 10 seconds have already elapsed, wait up to 10 more seconds for the
      # queries to be written to the completed queries table.
      self.cluster.get_first_impalad().service.wait_for_metric_value(
        "impala-server.completed-queries.written", query_count, 10)
    finally:
      client.close()

  @CustomClusterTestSuite.with_args(impalad_args="--enable_workload_mgmt "
                                                 "--query_log_write_interval_s=9999 "
                                                 "--shutdown_grace_period_s=30 "
                                                 "--shutdown_deadline_s=30",
                                    catalogd_args="--enable_workload_mgmt",
                                    impalad_graceful_shutdown=False)
  def test_query_log_table_flush_on_shutdown(self, vector):
    """Asserts that queries that have completed but are not yet written to the query
       log table are flushed to the table before the coordinator exits."""

    impalad = self.cluster.get_first_impalad()
    client = self.create_impala_client(protocol=vector.get_value('protocol'))

    try:
      # Execute sql statements to ensure all get written to the query log table.
      sql1 = client.execute("select 1")
      assert sql1.success

      sql2 = client.execute("select 2")
      assert sql2.success

      sql3 = client.execute("select 3")
      assert sql3.success

      impalad.service.wait_for_metric_value("impala-server.completed-queries.queued", 3,
          60)

      impalad.kill_and_wait_for_exit(SIGRTMIN)

      client2 = self.create_client_for_nth_impalad(1, vector.get_value('protocol'))

      def assert_func(last_iteration):
        results = client2.execute("select query_id,sql from {0} where query_id in "
                                  "('{1}','{2}','{3}')".format(self.QUERY_TBL,
                                  sql1.query_id, sql2.query_id, sql3.query_id))

        success = len(results.data) == 3
        if last_iteration:
          assert len(results.data) == 3

        return success

      assert retry(func=assert_func, max_attempts=5, sleep_time_s=5)
    finally:
      client.close()
      client2.close()


class TestQueryLogTableAll(TestQueryLogTableBase):
  """Tests to assert the query log table is correctly populated when using all the
     client protocols."""

  SCRATCH_DIR = tempfile.mkdtemp(prefix="scratch_dir")

  @CustomClusterTestSuite.with_args(impalad_args="--enable_workload_mgmt "
                                                 "--query_log_write_interval_s=1 "
                                                 "--cluster_id=test_query_hist_2 "
                                                 "--shutdown_grace_period_s=10 "
                                                 "--shutdown_deadline_s=60",
                                    catalogd_args="--enable_workload_mgmt",
                                    impalad_graceful_shutdown=True)
  def test_query_log_table_ddl(self, vector):
    """Asserts the values written to the query log table match the values from the
       query profile for a DDL query."""
    tbl_name = "default.test_query_log_ddl_" + str(int(time()))
    create_tbl_sql = "create table {0} (id INT, product_name STRING) " \
        "partitioned by (category INT)".format(tbl_name)
    client = self.create_impala_client(protocol=vector.get_value('protocol'))

    try:
      res = client.execute(create_tbl_sql, fetch_profile_after_close=True)
      assert res.success

      self.cluster.get_first_impalad().service.wait_for_metric_value(
          "impala-server.completed-queries.written", 1, 60)

      client2 = self.create_client_for_nth_impalad(2, vector.get_value('protocol'))
      assert client2 is not None
      assert_query(self.QUERY_TBL, client2, "test_query_hist_2", res.runtime_profile)
    finally:
      client.execute("drop table if exists {0}".format(tbl_name))
      client.close()

  @CustomClusterTestSuite.with_args(impalad_args="--enable_workload_mgmt "
                                                 "--query_log_write_interval_s=1 "
                                                 "--cluster_id=test_query_hist_3 "
                                                 "--shutdown_grace_period_s=10 "
                                                 "--shutdown_deadline_s=60",
                                    catalogd_args="--enable_workload_mgmt",
                                    impalad_graceful_shutdown=True)
  def test_query_log_table_dml(self, vector):
    """Asserts the values written to the query log table match the values from the
       query profile for a DML query."""
    tbl_name = "default.test_query_log_dml_" + str(int(time()))
    create_tbl_sql = "create table {0} (id INT, product_name STRING) " \
        "partitioned by (category INT)".format(tbl_name)
    client = self.create_impala_client(protocol=vector.get_value('protocol'))

    try:
      # Create the test table.
      create_tbl_sql = "create table {0} (id INT, product_name STRING) " \
        "partitioned by (category INT)".format(tbl_name)
      create_tbl_results = client.execute(create_tbl_sql)
      assert create_tbl_results.success

      insert_sql = "insert into {0} (id,category,product_name) values " \
                   "(0,1,'the product')".format(tbl_name)
      res = client.execute(insert_sql, fetch_profile_after_close=True)
      assert res.success

      self.cluster.get_first_impalad().service.wait_for_metric_value(
          "impala-server.completed-queries.written", 2, 60)

      client2 = self.create_client_for_nth_impalad(2, vector.get_value('protocol'))
      assert client2 is not None
      assert_query(self.QUERY_TBL, client2, "test_query_hist_3", res.runtime_profile)
    finally:
      client.execute("drop table if exists {0}".format(tbl_name))
      client.close()

  @CustomClusterTestSuite.with_args(impalad_args="--enable_workload_mgmt "
                                                 "--query_log_write_interval_s=1 "
                                                 "--cluster_id=test_query_hist_1 "
                                                 "--shutdown_grace_period_s=10 "
                                                 "--shutdown_deadline_s=60 "
                                                 "--scratch_dirs={0}:5G"
                                                 .format(SCRATCH_DIR),
                                    catalogd_args="--enable_workload_mgmt",
                                    impalad_graceful_shutdown=True)
  @pytest.mark.parametrize("buffer_pool_limit", [(None), ("16.05MB")])
  def test_query_log_table_query_select(self, vector, buffer_pool_limit):
    """Asserts the values written to the query log table match the values from the
       query profile. If the buffer_pool_limit parameter is not None, then this test
       requires that the query spills to disk to assert that the spill metrics are correct
       in the completed queries table."""
    tbl_name = "default.test_query_log_" + str(int(time()))
    client = self.create_impala_client(protocol=vector.get_value('protocol'))
    query_cnt = 0

    try:
      # Create the test table.
      create_tbl_sql = "create table {0} (id INT, product_name STRING, create_dt STRING" \
          ",descr STRING) partitioned by (category INT)".format(tbl_name)
      print("CREATE TABLE SQL: {0}".format(create_tbl_sql))
      create_tbl_results = client.execute(create_tbl_sql)
      assert create_tbl_results.success
      query_cnt += 1

      # Insert some rows into the test table.
      def __run_insert(values):
        insert_results = client.execute("insert into {0} (id,category,product_name,"
        "create_dt,descr) VALUES ({1})".format(tbl_name, values))
        assert insert_results.success

      # When buffer pool limit is not None, the test is forcing the query to spill. Thus,
      # a large number of records is needed to force the spilling.
      record_count_to_insert = 99
      if buffer_pool_limit is not None:
        record_count_to_insert = 24999

      insert_vals = ""
      for i in range(1, record_count_to_insert):
        random_product_name = "".join(choice(string.ascii_letters) for _ in range(100))
        random_dt = "{:}-{:0>2}-{:0>2}".format(randint(1982, 2022), randint(1, 12),
            randint(1, 31))
        random_desc = "".join(choice(string.ascii_letters) for _ in range(1000))
        insert_vals += "({0},{1},'{2}','{3}','{4}'),".format(i, (i % 50),
           random_product_name, random_dt, random_desc)

        if i % 500 == 0:
          __run_insert(insert_vals[:-1])
          query_cnt += 1
          insert_vals = ""

      __run_insert(insert_vals[:-1])
      query_cnt += 1

      # Set up query configuration
      client.set_configuration_option("MAX_MEM_ESTIMATE_FOR_ADMISSION", "10MB")
      if buffer_pool_limit is not None:
        client.set_configuration_option("BUFFER_POOL_LIMIT", buffer_pool_limit)
        client.set_configuration_option("SPOOL_QUERY_RESULTS", "TRUE")

      # Select all rows from the test table.
      res = client.execute("select * from {0} order by create_dt".format(tbl_name),
          fetch_profile_after_close=True)
      assert res.success
      query_cnt += 1

      self.cluster.get_first_impalad().service.wait_for_metric_value(
          "impala-server.completed-queries.written", query_cnt, 60)

      client2 = self.create_client_for_nth_impalad(2, vector.get_value('protocol'))
      assert client2 is not None
      data = assert_query(self.QUERY_TBL, client2, "test_query_hist_1",
          res.runtime_profile, max_mem_for_admission=10485760)

      if buffer_pool_limit is not None:
        # Since the assert_query function only asserts that the compressed bytes spilled
        # column is equal to the compressed bytes spilled in the profile, there is a
        # potential for this test to not actually assert anything different than other
        # tests. Thus, an additional assert is needed to ensure that there actually was
        # data that was spilled.
        assert data[COMPRESSED_BYTES_SPILLED] != "0", "compressed bytes spilled total " \
            "was zero, test did not assert anything"
    finally:
      client.execute("drop table if exists {0}".format(tbl_name))
      client.close()

  @CustomClusterTestSuite.with_args(impalad_args="--enable_workload_mgmt "
                                                 "--query_log_write_interval_s=1 "
                                                 "--cluster_id=test_query_hist_2 "
                                                 "--shutdown_grace_period_s=10 "
                                                 "--shutdown_deadline_s=60 ",
                                    catalogd_args="--enable_workload_mgmt",
                                    impalad_graceful_shutdown=True)
  def test_query_log_table_invalid_query(self, vector):
    """Asserts correct values are written to the completed queries table for a failed
       query. The query profile is used as the source of expected values."""
    client = self.create_impala_client(protocol=vector.get_value('protocol'))

    # Assert an invalid query
    unix_now = time()
    try:
      client.execute("{0}".format(unix_now))
    except Exception as _:
      pass

    # Get the query id from the completed queries table since the call to execute errors
    # instead of return the results object which contains the query id.
    impalad = self.cluster.get_first_impalad()
    impalad.service.wait_for_metric_value("impala-server.completed-queries.written", 1,
        60)

    result = client.execute("select query_id from {0} where sql='{1}'"
                            .format(self.QUERY_TBL, unix_now),
                            fetch_profile_after_close=True)
    assert result.success
    assert len(result.data) == 1

    assert_query(query_tbl=self.QUERY_TBL, client=client,
        expected_cluster_id="test_query_hist_2", impalad=impalad, query_id=result.data[0])

  @CustomClusterTestSuite.with_args(impalad_args="--enable_workload_mgmt "
                                                 "--query_log_write_interval_s=1 "
                                                 "--shutdown_grace_period_s=10 "
                                                 "--shutdown_deadline_s=60",
                                    catalogd_args="--enable_workload_mgmt",
                                    impalad_graceful_shutdown=True)
  def test_query_log_ignored_sqls(self, vector):
    """Asserts that expected queries are not written to the query log table."""
    client = self.create_impala_client(protocol=vector.get_value('protocol'))

    sqls = {}
    sqls["use default"] = False
    sqls["USE default"] = False
    sqls["uSe default"] = False
    sqls["--mycomment\nuse default"] = False
    sqls["/*mycomment*/ use default"] = False

    sqls["set all"] = False
    sqls["SET all"] = False
    sqls["SeT all"] = False
    sqls["--mycomment\nset all"] = False
    sqls["/*mycomment*/ set all"] = False

    sqls["show tables"] = False
    sqls["SHOW tables"] = False
    sqls["ShoW tables"] = False
    sqls["ShoW create table {0}".format(self.QUERY_TBL)] = False
    sqls["show databases"] = False
    sqls["SHOW databases"] = False
    sqls["ShoW databases"] = False
    sqls["show schemas"] = False
    sqls["SHOW schemas"] = False
    sqls["ShoW schemas"] = False
    sqls["--mycomment\nshow tables"] = False
    sqls["/*mycomment*/ show tables"] = False
    sqls["/*mycomment*/ show tables"] = False
    sqls["/*mycomment*/ show create table {0}".format(self.QUERY_TBL)] = False
    sqls["/*mycomment*/ show files in {0}".format(self.QUERY_TBL)] = False
    sqls["/*mycomment*/ show functions"] = False
    sqls["/*mycomment*/ show data sources"] = False
    sqls["/*mycomment*/ show views"] = False

    sqls["describe database default"] = False
    sqls["/*mycomment*/ describe database default"] = False
    sqls["describe {0}".format(self.QUERY_TBL)] = False
    sqls["/*mycomment*/ describe {0}".format(self.QUERY_TBL)] = False
    sqls["describe history {0}".format(self.QUERY_TBL)] = False
    sqls["/*mycomment*/ describe history {0}".format(self.QUERY_TBL)] = False
    sqls["select 1"] = True

    control_queries_count = 0
    try:
      for sql, experiment_control in sqls.items():
        results = client.execute(sql)
        assert results.success, "could not execute query '{0}'".format(sql)
        sqls[sql] = results.query_id

        # Ensure at least one sql statement was written to the completed queries table
        # to avoid false negatives where the sql statements that are ignored are not
        # written to the completed queries table because of another issue.
        if experiment_control:
          control_queries_count += 1
          sql_results = None
          for _ in range(6):
            sql_results = client.execute("select * from {0} where query_id='{1}'".format(
              self.QUERY_TBL, results.query_id))
            control_queries_count += 1
            if sql_results.success and len(sql_results.data) == 1:
              break
            else:
              sleep(5)
          assert sql_results.success
          assert len(sql_results.data) == 1, "query not found in completed queries table"
          sqls.pop(sql)

      for sql, query_id in sqls.items():
        log_results = client.execute("select * from {0} where query_id='{1}'"
                                     .format(self.QUERY_TBL, query_id))
        assert log_results.success
        assert len(log_results.data) == 0, "found query in query log table: {0}" \
                                               .format(sql)
    finally:
      client.close()

    # Assert there was one query per sql item written to the query log table. The queries
    # inserted into the completed queries table are the queries used to assert the ignored
    # queries were not written to the table.
    self.cluster.get_first_impalad().service.wait_for_metric_value(
        "impala-server.completed-queries.written", len(sqls) + control_queries_count, 60)
    assert self.cluster.get_first_impalad().service.get_metric_value(
        "impala-server.completed-queries.failure") == 0

  @CustomClusterTestSuite.with_args(impalad_args="--enable_workload_mgmt "
                                                 "--query_log_write_interval_s=1 "
                                                 "--shutdown_grace_period_s=10 "
                                                 "--shutdown_deadline_s=60",
                                    catalogd_args="--enable_workload_mgmt",
                                    impalad_graceful_shutdown=True)
  def test_query_log_table_sql_injection(self, vector):
    tbl_name = "default.test_query_log_sql_injection_" + str(int(time()))
    client = self.create_impala_client(protocol=vector.get_value('protocol'))

    try:
      # Create the test table.
      create_tbl_sql = "create table {0} (id INT, product_name STRING) " \
        "partitioned by (category INT)".format(tbl_name)
      create_tbl_results = client.execute(create_tbl_sql)
      assert create_tbl_results.success

      # Insert some rows into the test table.
      insert_sql = "insert into {0} (id,category,product_name) VALUES ".format(tbl_name)
      for i in range(1, 11):
        for j in range(1, 11):
          if i * j > 1:
            insert_sql += ","

          insert_sql += "({0},{1},'{2}')".format((i * j), i,
                                                  "product-{0}-{1}".format(i, j))

      insert_results = client.execute(insert_sql)
      assert insert_results.success

      impalad = self.cluster.get_first_impalad()

      # Try a sql injection attack with closing quotes.
      sql1_str = "select * from {0} where product_name='product-2-3'".format(tbl_name)
      self.__run_sql_inject(impalad, client, sql1_str, "closing quotes", 3)

      # Try a sql inject attack with terminating quote and semicolon.
      sql2_str = "select 1'); drop table {0}; select('" \
                 .format(self.QUERY_TBL)
      self.__run_sql_inject(impalad, client, sql2_str, "terminating semicolon", 6)

      # Attempt to cause an error using multiline comments.
      sql3_str = "select 1' /* foo"
      self.__run_sql_inject(impalad, client, sql3_str, "multiline comments", 9, False)

      # Attempt to cause an error using single line comments.
      sql4_str = "select 1' -- foo"
      self.__run_sql_inject(impalad, client, sql4_str, "single line comments", 12, False)

    finally:
      client.execute("drop table if exists {0}".format(tbl_name))
      client.close()

  def __run_sql_inject(self, impalad, client, sql, test_case, expected_writes,
                       expect_success=True):
    sql_result = None
    try:
      sql_result = client.execute(sql)
    except Exception as e:
      if expect_success:
        raise e

    if expect_success:
      assert sql_result.success

    impalad.service.wait_for_metric_value(
        "impala-server.completed-queries.written", expected_writes, 60)

    # Force Impala to process the inserts to the completed queries table.
    client.execute("refresh " + self.QUERY_TBL)

    if expect_success:
      sql_verify = client.execute(
          "select sql from {0} where query_id='{1}'"
          .format(self.QUERY_TBL, sql_result.query_id))

      assert sql_verify.success, test_case
      assert len(sql_verify.data) == 1, "did not find query '{0}' in query log " \
                                        "table for test case '{1}" \
                                        .format(sql_result.query_id, test_case)
      assert sql_verify.data[0] == sql, test_case
    else:
      esc_sql = sql.replace("'", "\\'")
      sql_verify = client.execute("select sql from {0} where sql='{1}' "
                                  "and start_time_utc > "
                                  "date_sub(utc_timestamp(), interval 25 seconds);"
                                  .format(self.QUERY_TBL, esc_sql))
      assert sql_verify.success, test_case
      assert len(sql_verify.data) == 1, "did not find query '{0}' in query log " \
                                        "table for test case '{1}" \
                                        .format(esc_sql, test_case)
