try:
    from itertools import izip as zip
except ImportError:
    # python 3
    pass

import numpy as np

import pytesmo.scaling as scaling
from pytesmo.validation_framework.data_manager import DataManager
from pytesmo.validation_framework.data_manager import get_result_names
import pytesmo.validation_framework.temporal_matchers as temporal_matchers


class Validation(object):

    """
    Class for the validation process.

    Parameters
    ----------
    datasets : dict of dicts
        Keys: string, datasets names
        Values: dict, containing the following fields
            'class': object
                Class containing the method read_ts for reading the data.
            'columns': list
                List of columns which will be used in the validation process.
            'type': string
                'reference' or 'other'. If the dataset is the reference it will be used
                as a spatial, temporal and scaling reference. temporal and scaling references
                can be changed if needed. See the optional parameters ``temporal_ref`` and
                ``scaling_ref``.
            'args': list, optional
                Args for reading the data.
            'kwargs': dict, optional
                Kwargs for reading the data
            'grids_compatible': boolean, optional
                If set to True the grid point index is used directly when
                reading other, if False then lon, lat is used and a nearest
                neighbour search is necessary.
            'use_lut': boolean, optional
                If set to True the grid point index (obtained from a
                calculated lut between reference and other) is used when
                reading other, if False then lon, lat is used and a
                nearest neighbour search is necessary.
            'lut_max_dist': float, optional
                Maximum allowed distance in meters for the lut calculation.
    metrics_calculators : dict of functions
        The keys of the dict are tuples with the following structure: (n, k) with n >= 2
        and n>=k. n is the number of datasets that should be temporally matched to the
        reference dataset and k is how many columns the metric calculator will get at once.
        What this means is that it is e.g. possible to temporally match 3 datasets with
        3 columns in total and then give the combinations of these columns to the metric
        calculator in sets of 2 by specifying the dictionary like:

        .. code::

            { (3, 2): metric_calculator}

        The values are functions that take an input DataFrame with the columns 'ref'
        for the reference and 'n1', 'n2' and
        so on for other datasets as well as a dictionary mapping the column names
        to the names of the original datasets. In this way multiple metric calculators
        can be applied to different combinations of n input datasets.
    temporal_matcher: function, optional
        function that takes a dict of dataframes and a reference_key.
        It performs the temporal matching on the data and returns a dictionary
        of matched DataFrames that should be evaluated together by the metric calculator.
    temporal_window: float, optional
        Window to allow in temporal matching in days. The window is allowed on both
        sides of the timestamp of the temporal reference data.
        Only used with the standard temporal matcher.
    temporal_ref: string, optional
        If the temporal matching should use another dataset than the spatial reference
        as a reference dataset then give the dataset name here.
    data_prep: object, optional
        Object that provides the methods prep_reference and prep_other
        which take the pandas.Dataframe provided by the read_ts methods (plus
        other_name for prep_other) and do some data preparation on it before
        temporal matching etc. can be used e.g. for special masking or anomaly
        calculations.
    period : list, optional
        Of type [datetime start, datetime end]. If given then the two input
        datasets will be truncated to start <= dates <= end.
    scaling : string
        If set then the data will be scaled into the reference space using the
        method specified by the string.
    scaling_ref : string, optional
        If the scaling should be done to another dataset than the spatial reference then
        give the dataset name here.
    cell_based_jobs : boolean, optional
        If True then the jobs will be cell based, if false jobs will be tuples
        of (gpi, lon, lat).

    Methods
    -------
    calc(job)
        Takes either a cell or a gpi_info tuple and performs the validation.
    get_processing_jobs()
        Returns processing jobs that this process can understand.
    """

    def __init__(self, datasets, metrics_calculators,
                 temporal_matcher=None, temporal_window=1 / 24.0,
                 temporal_ref=None, data_prep=None, period=None, scaling='lin_cdf_match',
                 scaling_ref=None, cell_based_jobs=True):
        """
        Initialize parameters.
        """
        self.data_manager = DataManager(datasets, data_prep, period)

        self.temp_matching = temporal_matcher
        if self.temp_matching is None:
            self.temp_matching = temporal_matchers.BasicTemporalMatching(
                window=temporal_window).combinatory_matcher

        self.temporal_ref = temporal_ref
        if self.temporal_ref is None:
            self.temporal_ref = self.data_manager.reference_name

        self.metrics_c = metrics_calculators

        self.scaling = scaling
        self.scaling_ref = scaling_ref
        if self.scaling_ref is None:
            self.scaling_ref = self.data_manager.reference_name

        self.cell_based_jobs = cell_based_jobs

        self.luts = self.data_manager.get_luts()

    def calc(self, job):
        """
        Takes either a cell or a gpi_info tuple and performs the validation.

        Parameters
        ----------
        job : object
            Job of type that self.get_processing_jobs() returns.

        Returns
        -------
        compact_results : dict of dicts
            Keys: result names, combinations of
                  (referenceDataset.column, otherDataset.column)
            Values: dict containing the elements returned by metrics_calculator
        """
        results = {}

        if self.cell_based_jobs:
            process_gpis, process_lons, process_lats = self.data_manager.\
                reference_grid.grid_points_for_cell(job)
        else:
            process_gpis, process_lons, process_lats = [
                job[0]], [job[1]], [job[2]]

        for gpi_info in zip(process_gpis, process_lons, process_lats):
            # if processing is cell based gpi_metainfo is limited to gpi, lon,
            # lat at the moment
            if self.cell_based_jobs:
                gpi_meta = gpi_info
            else:
                gpi_meta = job

            df_dict = self.data_manager.get_data(gpi_info[0],
                                                 gpi_info[1],
                                                 gpi_info[2])

            # if no data is available continue with the next gpi
            if len(df_dict) == 0:
                continue
            # compute results for combinations as requested by the metrics
            # calculator dict
            # First temporal match all the combinations
            matched_n = {}
            for n, k in self.metrics_c:
                matched_data = self.temp_matching(df_dict,
                                                  self.temporal_ref,
                                                  n=n)

                matched_n[(n, k)] = matched_data

            for n, k in self.metrics_c:
                n_matched_data = matched_n[(n, k)]
                for result in get_result_names(self.data_manager.ds_dict,
                                               self.temporal_ref,
                                               n=k):
                    # find the key into the temporally matched dataset by combining the
                    # dataset parts of the result_names
                    dskey = []
                    rename_dict = {}
                    f = lambda x: "k{}".format(x) if x > 0 else 'ref'
                    for i, r in enumerate(result):
                        dskey.append(r[0])
                        rename_dict[r[0]] = f(i)

                    dskey = tuple(dskey)
                    if n == k:
                        # we should have an exact match of datasets and
                        # temporal matches
                        data = n_matched_data[dskey]
                    else:
                        # more datasets were temporally matched than are
                        # requested now so we select a temporally matched
                        # dataset that has the first key in common with the
                        # requested one ensuring that it was used as a
                        # reference and also has the rest of the requested
                        # datasets in the key
                        first_match = [
                            key for key in n_matched_data if dskey[0] == key[0]]
                        found_key = None
                        for key in first_match:
                            for dsk in dskey[1:]:
                                if dsk not in key:
                                    continue
                            found_key = key
                        data = n_matched_data[found_key]

                    # extract only the relevant columns from matched DataFrame
                    data = data[[x for x in result]]

                    # at this stage we can drop the column multiindex and just use
                    # the dataset name
                    data.columns = data.columns.droplevel(level=1)

                    data.rename(columns=rename_dict, inplace=True)

                    if len(data) == 0:
                        continue

                    if self.scaling is not None:
                        # get scaling index by finding the column in the
                        # DataFrame that belongs to the scaling reference
                        scaling_index = data.columns.tolist().index(
                            rename_dict[self.scaling_ref])
                        try:
                            data = scaling.scale(data,
                                                 method=self.scaling,
                                                 reference_index=scaling_index)
                        except ValueError:
                            continue

                    if result not in results.keys():
                        results[result] = []

                    metrics_calculator = self.metrics_c[(n, k)]
                    results[result].append(metrics_calculator(data, gpi_meta))

        compact_results = {}
        for key in results.keys():
            compact_results[key] = {}
            for field_name in results[key][0].keys():
                entries = []
                for result in results[key]:
                    entries.append(result[field_name][0])
                compact_results[key][field_name] = \
                    np.array(entries, dtype=results[key][0][field_name].dtype)

        return compact_results

    def get_processing_jobs(self):
        """
        Returns processing jobs that this process can understand.

        Returns
        -------
        jobs : list
            List of cells or gpis to process.
        """
        if self.data_manager.reference_grid is not None:
            if self.cell_based_jobs:
                return self.data_manager.reference_grid.get_cells()
            else:
                return zip(self.data_manager.reference_grid.get_grid_points())
        else:
            return []
