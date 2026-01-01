import json

class DbFiles:
    min_amount_fname = 'db/min_amount.json'
    greed_fname = 'db/greed.json'

    @staticmethod
    def write(data, fname):
        with open(fname, 'w') as outfile:
            json.dump(data, outfile, indent=2)

    @staticmethod
    def read(fname):
        with open(fname, 'r') as f:
            return json.loads(f.read())

    @staticmethod
    def write_greed(i_greed, strat_id):
        greeds = DbFiles.read(DbFiles.greed_fname)
        try:
            greed = next(item for item in greeds if item['strat_id'] == strat_id)
            greed['greed'] = i_greed
        except StopIteration:
            greed = {'strat_id': strat_id, 'greed': i_greed}
            greeds.append(greed)
        DbFiles.write(greeds, DbFiles.greed_fname)


    @staticmethod
    def read_greed(strat_id):
        greeds = DbFiles.read(DbFiles.greed_fname)
        for greed in greeds:
            if greed['strat_id'] == strat_id:
                return greed['greed']
        return []
