"""
**results** module provides the logic to format, save and read predictions generated by the *automl frameworks* (cf. ``TaskResult``),
as well as logic to compute, format, save, read and merge scores obtained from those predictions (cf. ``Result`` and ``Scoreboard``).
"""
from functools import partial
import collections
import io
import logging
import math
import os
import re
import statistics

import numpy as np
from numpy import nan, sort
import pandas as pd

from .data import Dataset, DatasetType, Feature
from .datautils import accuracy_score, confusion_matrix, f1_score, log_loss, balanced_accuracy_score, mean_absolute_error, mean_squared_error, mean_squared_log_error, r2_score, roc_auc_score, read_csv, write_csv, is_data_frame, to_data_frame
from .resources import get as rget, config as rconfig, output_dirs
from .utils import Namespace, backup_file, cached, datetime_iso, json_load, memoize, profile

log = logging.getLogger(__name__)


class NoResultError(Exception):
    pass


class ResultError(Exception):
    pass

# TODO: reconsider organisation of output files:
#   predictions: add framework version to name, timestamp? group into subdirs?


class Scoreboard:

    results_file = 'results.csv'

    @classmethod
    def all(cls, scores_dir=None):
        return cls(scores_dir=scores_dir)

    @classmethod
    def from_file(cls, path):
        sep = rconfig().token_separator
        folder, basename = os.path.split(path)
        framework_name = None
        benchmark_name = None
        task_name = None
        patterns = [
            cls.results_file,
            rf"(?P<framework>[\w\-]+){sep}benchmark{sep}(?P<benchmark>[\w\-]+)\.csv",
            rf"benchmark{sep}(?P<benchmark>[\w\-]+)\.csv",
            rf"(?P<framework>[\w\-]+){sep}task{sep}(?P<task>[\w\-]+)\.csv",
            rf"task{sep}(?P<task>[\w\-]+)\.csv",
            r"(?P<framework>[\w\-]+)\.csv",
        ]
        found = False
        for pat in patterns:
            m = re.fullmatch(pat, basename)
            if m:
                found = True
                d = m.groupdict()
                benchmark_name = 'benchmark' in d and d['benchmark']
                task_name = 'task' in d and d['task']
                framework_name = 'framework' in d and d['framework']
                break

        if not found:
            return None

        scores_dir = None if path == basename else folder
        return cls(framework_name=framework_name, benchmark_name=benchmark_name, task_name=task_name, scores_dir=scores_dir)

    @staticmethod
    # @profile(logger=log)
    def load_df(file):
        name = file if isinstance(file, str) else type(file)
        log.debug("Loading scores from `%s`.", name)
        exists = isinstance(file, io.IOBase) or os.path.isfile(file)
        df = read_csv(file) if exists else to_data_frame({})
        log.debug("Loaded scores from `%s`.", name)
        return df

    @staticmethod
    # @profile(logger=log)
    def save_df(data_frame, path, append=False):
        exists = os.path.isfile(path)
        new_format = False
        if exists:
            df = read_csv(path, nrows=1)
            new_format = list(df.columns) != list(data_frame.columns)
        if new_format or (exists and not append):
            backup_file(path)
        new_file = not exists or not append or new_format
        is_default_index = data_frame.index.name is None and not any(data_frame.index.names)
        log.debug("Saving scores to `%s`.", path)
        write_csv(data_frame,
                  path=path,
                  header=new_file,
                  index=not is_default_index,
                  append=not new_file)
        log.info("Scores saved to `%s`.", path)

    def __init__(self, scores=None, framework_name=None, benchmark_name=None, task_name=None, scores_dir=None):
        self.framework_name = framework_name
        self.benchmark_name = benchmark_name
        self.task_name = task_name
        self.scores_dir = (scores_dir if scores_dir
                           else output_dirs(rconfig().output_dir, rconfig().sid, ['scores']).scores)
        self.scores = scores if scores is not None else self._load()

    @cached
    def as_data_frame(self):
        # index = ['task', 'framework', 'fold']
        index = []
        df = (self.scores if is_data_frame(self.scores)
              else to_data_frame([dict(sc) for sc in self.scores]))
        if df.empty:
            # avoid dtype conversions during reindexing on empty frame
            return df
        fixed_cols = ['id', 'task', 'framework', 'constraint', 'fold', 'result', 'metric', 'mode', 'version',
                      'params', 'app_version', 'utc', 'duration', 'training_duration', 'predict_duration', 'models_count', 'seed', 'info']
        fixed_cols = [col for col in fixed_cols if col not in index]
        dynamic_cols = [col for col in df.columns if col not in index and col not in fixed_cols]
        dynamic_cols.sort()
        df = df.reindex(columns=[]+fixed_cols+dynamic_cols)
        log.debug("Scores columns: %s.", df.columns)
        return df

    @cached
    def as_printable_data_frame(self):
        str_print = lambda val: '' if val in [None, '', 'None'] or (isinstance(val, float) and np.isnan(val)) else val
        int_print = lambda val: int(val) if isinstance(val, float) and not np.isnan(val) else str_print(val)
        num_print = lambda fn, val: None if isinstance(val, str) else fn(val)

        df = self.as_data_frame()
        force_str_cols = ['id']
        nanable_int_cols = ['fold', 'models_count', 'seed']
        low_precision_float_cols = ['duration', 'training_duration', 'predict_duration']
        high_precision_float_cols = [col for col in df.select_dtypes(include=[np.float]).columns if col not in ([] + nanable_int_cols + low_precision_float_cols)]
        for col in force_str_cols:
            df[col] = df[col].astype(np.object).map(str_print).astype(np.str)
        for col in nanable_int_cols:
            df[col] = df[col].astype(np.object).map(int_print).astype(np.str)
        for col in low_precision_float_cols:
            df[col] = df[col].astype(np.float).map(partial(num_print, "{:.1f}".format)).astype(np.float)
        for col in high_precision_float_cols:
            df[col] = df[col].map(partial(num_print, "{:.6g}".format)).astype(np.float)
        return df

    def _load(self):
        return self.load_df(self._score_file())

    def save(self, append=False):
        self.save_df(self.as_printable_data_frame(), path=self._score_file(), append=append)

    def append(self, board_or_df, no_duplicates=True):
        to_append = board_or_df.as_data_frame() if isinstance(board_or_df, Scoreboard) else board_or_df
        scores = self.as_data_frame().append(to_append, sort=False)
        if no_duplicates:
            scores = scores.drop_duplicates()
        return Scoreboard(scores=scores,
                          framework_name=self.framework_name,
                          benchmark_name=self.benchmark_name,
                          task_name=self.task_name,
                          scores_dir=self.scores_dir)

    def _score_file(self):
        sep = rconfig().token_separator
        if self.framework_name:
            if self.task_name:
                file_name = f"{self.framework_name}{sep}task_{self.task_name}.csv"
            elif self.benchmark_name:
                file_name = f"{self.framework_name}{sep}benchmark_{self.benchmark_name}.csv"
            else:
                file_name = f"{self.framework_name}.csv"
        else:
            if self.task_name:
                file_name = f"task_{self.task_name}.csv"
            elif self.benchmark_name:
                file_name = f"benchmark_{self.benchmark_name}.csv"
            else:
                file_name = Scoreboard.results_file

        return os.path.join(self.scores_dir, file_name)


class TaskResult:

    @staticmethod
    # @profile(logger=log)
    def load_predictions(predictions_file):
        log.info("Loading predictions from `%s`.", predictions_file)
        if os.path.isfile(predictions_file):
            try:
                df = read_csv(predictions_file, dtype=object)
                log.debug("Predictions preview:\n %s\n", df.head(10).to_string())
                if rconfig().test_mode:
                    TaskResult.validate_predictions(df)
                if df.shape[1] > 2:
                    return ClassificationResult(df)
                else:
                    return RegressionResult(df)
            except Exception as e:
                return ErrorResult(ResultError(e))
        else:
            log.warning("Predictions file `%s` is missing: framework either failed or could not produce any prediction.", predictions_file)
            return NoResult("Missing predictions.")

    @staticmethod
    def load_metadata(metadata_file):
        log.info("Loading metadata from `%s`.", metadata_file)
        if os.path.isfile(metadata_file):
            return json_load(metadata_file, as_namespace=True)
        else:
            log.warning("Metadata file `%s` is missing: framework either couldn't start or implementation doesn't save metadata.", metadata_file)
            return Namespace(lambda: None)

    @staticmethod
    # @profile(logger=log)
    def save_predictions(dataset: Dataset, output_file: str,
                         predictions=None, truth=None,
                         probabilities=None, probabilities_labels=None,
                         target_is_encoded=False,
                         preview=True):
        """ Save class probabilities and predicted labels to file in csv format.

        :param dataset:
        :param output_file:
        :param probabilities:
        :param predictions:
        :param truth:
        :param probabilities_labels:
        :param target_is_encoded:
        :param preview:
        :return: None
        """
        log.debug("Saving predictions to `%s`.", output_file)
        remap = None
        if probabilities is not None:
            prob_cols = probabilities_labels if probabilities_labels else dataset.target.label_encoder.classes
            df = to_data_frame(probabilities, columns=prob_cols)
            if probabilities_labels:
                df = df[sort(prob_cols)]  # reorder columns alphabetically: necessary to match label encoding
                if any(prob_cols != df.columns.values):
                    encoding_map = {prob_cols.index(col): i for i, col in enumerate(df.columns.values)}
                    remap = np.vectorize(lambda v: encoding_map[v])
        else:
            df = to_data_frame(None)

        preds = predictions
        truth = truth if truth is not None else dataset.test.y
        if not _encode_predictions_and_truth_ and target_is_encoded:
            if remap:
                predictions = remap(predictions)
                truth = remap(truth)
            preds = dataset.target.label_encoder.inverse_transform(predictions)
            truth = dataset.target.label_encoder.inverse_transform(truth)
        if _encode_predictions_and_truth_ and not target_is_encoded:
            preds = dataset.target.label_encoder.transform(predictions)
            truth = dataset.target.label_encoder.transform(truth)

        df = df.assign(predictions=preds)
        df = df.assign(truth=truth)
        if preview:
            log.info("Predictions preview:\n %s\n", df.head(20).to_string())
        backup_file(output_file)
        write_csv(df, path=output_file)
        log.info("Predictions saved to `%s`.", output_file)

    @staticmethod
    def validate_predictions(predictions: pd.DataFrame):
        names = predictions.columns.values
        assert len(names) >= 2, "predictions frame should have 2 columns (regression) or more (classification)"
        assert names[-1] == "truth", "last column of predictions frame must be named `truth`"
        assert names[-2] == "predictions", "last column of predictions frame must be named `predictions`"
        if len(names) == 2:  # regression
            for name, col in predictions.items():
                pd.to_numeric(col)  # pandas will raise if we have non-numerical values
        else:  # classification
            predictors = names[:-2]
            probabilities, preds, truth = predictions.iloc[:,:-2], predictions.iloc[:,-2], predictions.iloc[:,-1]
            assert np.array_equal(predictors, np.sort(predictors)), "Predictors columns are not sorted in lexicographic order."
            assert set(np.unique(predictors)) == set(predictors), "Predictions contain multiple columns with the same label."
            for name, col in probabilities.items():
                pd.to_numeric(col)  # pandas will raise if we have non-numerical values

            if _encode_predictions_and_truth_:
                assert np.array_equal(truth, truth.astype(int)), "Values in truth column are not encoded."
                assert np.array_equal(preds, preds.astype(int)), "Values in predictions column are not encoded."
                predictors_set = set(range(len(predictors)))
                validate_row = lambda r: r[:-2].astype(float).values.argmax() == r[-2]
            else:
                predictors_set = set(predictors)
                validate_row = lambda r: r[:-2].astype(float).idxmax() == r[-2]

            truth_set = set(truth.unique())
            if predictors_set < truth_set:
                log.warning("Truth column contains values unseen during training: no matching probability column.")
            if predictors_set > truth_set:
                log.warning("Truth column doesn't contain all the possible target values: the test dataset may be too small.")
            predictions_set = set(preds.unique())
            assert predictions_set <= predictors_set, "Predictions column contains unexpected values: {}.".format(predictions_set - predictors_set)
            assert predictions.apply(validate_row, axis=1).all(), "Predictions don't always match the predictor with the highest probability."

    @classmethod
    def score_from_predictions_file(cls, path):
        sep = rconfig().token_separator
        folder, basename = os.path.split(path)
        folder_g = collections.defaultdict(lambda: None)
        if folder:
            folder_pat = rf"/(?P<framework>[\w\-]+?){sep}(?P<benchmark>[\w\-]+){sep}(?P<constraint>[\w\-]+){sep}(?P<mode>[\w\-]+)({sep}(?P<datetime>\d{8}T\d{6}))/"
            folder_m = re.match(folder_pat, folder)
            if folder_m:
                folder_g = folder_m.groupdict()

        file_pat = rf"(?P<framework>[\w\-]+?){sep}(?P<task>[\w\-]+){sep}(?P<fold>\d+)\.csv"
        file_m = re.fullmatch(file_pat, basename)
        if not file_m:
            log.error("Predictions file `%s` has wrong naming format.", path)
            return None

        file_g = file_m.groupdict()
        framework_name = file_g['framework']
        task_name = file_g['task']
        fold = int(file_g['fold'])
        constraint = folder_g['constraint']
        benchmark = folder_g['benchmark']
        task = Namespace(name=task_name, id=task_name)
        if benchmark:
            try:
                tasks, _, _ = rget().benchmark_definition(benchmark)
                task = next(t for t in tasks if t.name==task_name)
            except:
                pass

        result = cls.load_predictions(path)
        task_result = cls(task, fold, constraint, '')
        metrics = rconfig().benchmarks.metrics[result.type.name]
        return task_result.compute_scores(framework_name, metrics, result=result)

    def __init__(self, task_def, fold: int, constraint: str, predictions_dir=None):
        self.task = task_def
        self.fold = fold
        self.constraint = constraint
        self.predictions_dir = (predictions_dir if predictions_dir
                                else output_dirs(rconfig().output_dir, rconfig().sid, ['predictions']).predictions)

    @memoize
    def get_result(self):
        return self.load_predictions(self._predictions_file)

    @memoize
    def get_metadata(self):
        return self.load_metadata(self._metadata_file)

    @profile(logger=log)
    def compute_scores(self, result=None, meta_result=None):
        meta_result = Namespace({} if meta_result is None else meta_result)
        metadata = self.get_metadata()
        scores = Namespace(
            id=self.task.id,
            task=self.task.name,
            constraint=self.constraint,
            framework=metadata.framework,
            version=metadata.version if 'version' in metadata else metadata.framework_version,
            params=repr(metadata.framework_params) if metadata.framework_params else '',
            fold=self.fold,
            mode=rconfig().run_mode,
            seed=metadata.seed,
            app_version=rget().app_version,
            utc=datetime_iso(),
            metric=metadata.metric,
            duration=nan
        )
        required_metares = ['training_duration', 'predict_duration', 'models_count']
        for m in required_metares:
            scores[m] = meta_result[m] if m in meta_result else nan
        result = self.get_result() if result is None else result

        scoring_errors = []

        def do_score(m):
            res = result.evaluate(m)
            print(m, res)
            score, err = res
            if err:
                scoring_errors.append(err)
            return score

        for metric in metadata.metrics or []:
            scores[metric] = do_score(metric)
        scores.result = scores[scores.metric] if scores.metric in scores else do_score(scores.metric)
        scores.info = result.info
        if scoring_errors:
            scores.info = "; ".join(filter(lambda it: it, [scores.info, *scoring_errors]))
        scores % Namespace({k: v for k, v in meta_result if k not in required_metares})
        log.info("Metric scores: %s", scores)
        return scores

    @property
    def _predictions_file(self):
        return os.path.join(self.predictions_dir, self.task.name, str(self.fold), "predictions.csv")

    @property
    def _metadata_file(self):
        return os.path.join(self.predictions_dir, self.task.name, str(self.fold), "metadata.json")


class Result:

    def __init__(self, predictions_df, info=None):
        self.df = predictions_df
        self.info = info
        self.truth = self.df.iloc[:, -1].values if self.df is not None else None
        self.predictions = self.df.iloc[:, -2].values if self.df is not None else None
        self.target = None
        self.type = None

    def evaluate(self, metric):
        if hasattr(self, metric):
            try:
                return getattr(self, metric)(), None
            except Exception as e:
                log.exception("Failed to compute metric %s: ", metric, e)
                return nan, f"scoring {metric}: {str(e)}"
        # raise ValueError("Metric {metric} is not supported for {type}.".format(metric=metric, type=self.type))
        log.warning("Metric %s is not supported for %s!", metric, self.type)
        return nan, f"Unsupported metric {metric} for {self.type}"


class NoResult(Result):

    def __init__(self, info=None):
        super().__init__(None, info)
        self.missing_result = np.nan

    def evaluate(self, metric):
        return self.missing_result, None


class ErrorResult(NoResult):

    def __init__(self, error):
        msg = "{}: {}".format(type(error).__qualname__ if error is not None else "Error", error)
        max_len = rconfig().results.error_max_length
        msg = msg if len(msg) <= max_len else (msg[:max_len - 3] + '...')
        super().__init__(msg)


class ClassificationResult(Result):

    def __init__(self, predictions_df, info=None):
        super().__init__(predictions_df, info)
        self.classes = self.df.columns[:-2].values.astype(str, copy=False)
        self.probabilities = self.df.iloc[:, :-2].values.astype(float, copy=False)
        self.target = Feature(0, 'class', 'categorical', values=self.classes, is_target=True)
        self.type = DatasetType.binary if len(self.classes) == 2 else DatasetType.multiclass
        self.truth = self._autoencode(self.truth.astype(str, copy=False))
        self.predictions = self._autoencode(self.predictions.astype(str, copy=False))
        self.labels = self._autoencode(self.classes)

    def acc(self):
        return float(accuracy_score(self.truth, self.predictions))

    def balacc(self):
        return float(balanced_accuracy_score(self.truth, self.predictions))

    def auc(self):
        if self.type != DatasetType.binary:
            # raise ValueError("AUC metric is only supported for binary classification: {}.".format(self.classes))
            log.warning("AUC metric is only supported for binary classification: %s.", self.labels)
            return nan
        return float(roc_auc_score(self.truth, self.probabilities[:, 1], labels=self.labels))

    def cm(self):
        return confusion_matrix(self.truth, self.predictions, labels=self.labels)

    def _per_class_errors(self):
        return [(s-d)/s for s, d in ((sum(r), r[i]) for i, r in enumerate(self.cm()))]

    def mean_pce(self):
        """mean per class error"""
        return statistics.mean(self._per_class_errors())

    def max_pce(self):
        """max per class error"""
        return max(self._per_class_errors())

    def f1(self):
        return float(f1_score(self.truth, self.predictions, labels=self.labels))

    def logloss(self):
        return float(log_loss(self.truth, self.probabilities, labels=self.labels))

    def _autoencode(self, vec):
        needs_encoding = not _encode_predictions_and_truth_ or (isinstance(vec[0], str) and not vec[0].isdigit())
        return self.target.label_encoder.transform(vec) if needs_encoding else vec


class RegressionResult(Result):

    def __init__(self, predictions_df, info=None):
        super().__init__(predictions_df, info)
        self.truth = self.truth.astype(float, copy=False)
        self.target = Feature(0, 'target', 'real', is_target=True)
        self.type = DatasetType.regression

    def mae(self):
        return float(mean_absolute_error(self.truth, self.predictions))

    def mse(self):
        return float(mean_squared_error(self.truth, self.predictions))

    def msle(self):
        return float(mean_squared_log_error(self.truth, self.predictions))

    def rmse(self):
        return math.sqrt(self.mse())

    def rmsle(self):
        return math.sqrt(self.msle())

    def r2(self):
        return float(r2_score(self.truth, self.predictions))


_encode_predictions_and_truth_ = False

save_predictions = TaskResult.save_predictions
